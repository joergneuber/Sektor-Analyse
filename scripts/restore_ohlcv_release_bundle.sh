#!/usr/bin/env bash
# Restore one verified OHLCV snapshot from the historical-ohlcv GitHub Release.
# Prefer immutable paired bundles; use the legacy pair only during migration.
set -euo pipefail

repo="${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"
tag="historical-ohlcv"
db_path="${NMM_HISTORICAL_OHLCV_DB:-historical_ohlcv.sqlite}"
universe_path="${NMM_HISTORICAL_OHLCV_DYNAMIC_UNIVERSE:-historical_ohlcv_universe.json}"
tmp_dir="$(mktemp -d)"
trap 'rm -rf "$tmp_dir"' EXIT

release_assets="$(bash scripts/get_gh_release_assets.sh "$repo" "$tag")"
printf '%s' "$release_assets" | jq -e 'type == "array"' >/dev/null
release_asset_count="$(printf '%s' "$release_assets" | jq 'length')"
release_bundle_count="$(printf '%s' "$release_assets" | jq '[.[] | select(.name | test("^historical_ohlcv_bundle_[A-Za-z0-9_.-]+\\.zip$"))] | length')"
legacy_db_present="$(printf '%s' "$release_assets" | jq -r 'any(.[]; .name == "historical_ohlcv.sqlite")')"
legacy_universe_present="$(printf '%s' "$release_assets" | jq -r 'any(.[]; .name == "historical_ohlcv_universe.json")')"
echo "OHLCV_RELEASE_METADATA | uploaded_assets=${release_asset_count} | bundles=${release_bundle_count} | legacy_db=${legacy_db_present} | legacy_universe=${legacy_universe_present} | legacy_migration_opt_in=${NMM_ALLOW_OHLCV_LEGACY_MIGRATION:-0}"
MAX_RELEASE_ASSET_BYTES=$((2 * 1024 * 1024 * 1024 - 1))

try_bundle_restore() {
  mapfile -t bundle_names < <(
    printf '%s' "$release_assets" |
      jq -r '[.[]
        | select(.name | test("^historical_ohlcv_bundle_[A-Za-z0-9_.-]+\\.zip$"))]
        | sort_by([.created_at // "", .name]) | reverse | .[].name'
  )

  for bundle_name in "${bundle_names[@]}"; do
    remote_digest="$(printf '%s' "$release_assets" | jq -r --arg name "$bundle_name" '[.[] | select(.name == $name)][0].digest // empty')"
    if ! [[ "$remote_digest" =~ ^sha256:[0-9a-fA-F]{64}$ ]]; then
      echo "::warning::Bundle ${bundle_name} hat keinen gültigen GitHub-SHA-256-Digest; übersprungen."
      continue
    fi
    remote_size="$(printf '%s' "$release_assets" | jq -r --arg name "$bundle_name" '[.[] | select(.name == $name)][0].size // empty')"
    if ! [[ "$remote_size" =~ ^[0-9]+$ ]] || (( remote_size > MAX_RELEASE_ASSET_BYTES )); then
      echo "::warning::Bundle ${bundle_name} fehlt eine gültige Asset-Größe oder es überschreitet die GitHub-Grenze (<2 GiB); übersprungen."
      continue
    fi
    rm -f "${tmp_dir:?}/${bundle_name}"
    if ! gh release download "$tag" --repo "$repo" --pattern "$bundle_name" --dir "$tmp_dir"; then
      echo "::warning::Bundle ${bundle_name} konnte nicht geladen werden; älteren Snapshot prüfen."
      continue
    fi
    local_size="$(stat -c '%s' "${tmp_dir}/${bundle_name}")"
    if (( local_size > MAX_RELEASE_ASSET_BYTES )) || [[ "$local_size" != "$remote_size" ]]; then
      echo "::warning::Heruntergeladenes Bundle ${bundle_name} überschreitet die GitHub-Grenze oder weicht in der Größe von den Release-Metadaten ab; übersprungen."
      rm -f "${tmp_dir:?}/${bundle_name}"
      continue
    fi
    local_digest="$(sha256sum "${tmp_dir}/${bundle_name}" | awk '{print $1}')"
    if [[ "$local_digest" != "${remote_digest#sha256:}" ]]; then
      echo "::warning::Digest-Abweichung bei ${bundle_name}; älteren Snapshot prüfen."
      rm -f "${tmp_dir:?}/${bundle_name}"
      continue
    fi
    if python ohlcv_release_bundle.py extract \
        --bundle "${tmp_dir}/${bundle_name}" \
        --db "$db_path" --universe "$universe_path"; then
      echo "OHLCV_RELEASE_RESTORE=PASS | bundle=${bundle_name}"
      return 0
    fi
    echo "::warning::Bundle ${bundle_name} besteht die Inhaltsprüfung nicht; älteren Snapshot prüfen."
  done
  return 1
}

if try_bundle_restore; then
  exit 0
fi

# Re-read once if all assets from the first metadata snapshot were absent or
# invalid. This closes the race where the main workflow publishes a bundle and
# prunes the legacy pair between our metadata request and our asset download.
release_assets="$(bash scripts/get_gh_release_assets.sh "$repo" "$tag")"
printf '%s' "$release_assets" | jq -e 'type == "array"' >/dev/null
MAX_RELEASE_ASSET_BYTES=$((2 * 1024 * 1024 * 1024 - 1))
if try_bundle_restore; then
  exit 0
fi

# Transitional fallback is disabled unless the repository variable is explicitly
# mapped into this workflow and set to 1 by the operator.
if [[ "${NMM_ALLOW_OHLCV_LEGACY_MIGRATION:-0}" != "1" ]]; then
  echo "::warning::Legacy-Fallback gesperrt: NMM_ALLOW_OHLCV_LEGACY_MIGRATION ist nicht ausdrücklich auf 1 gesetzt."
  echo "::error::Kein gültiger OHLCV-Bundle-Snapshot verfügbar; Legacy-Migration nicht freigegeben."
  exit 1
fi

# Legacy assets are accepted only with matching remote digests, sizes and timestamps.
db_digest="$(printf '%s' "$release_assets" | jq -r '[.[] | select(.name == "historical_ohlcv.sqlite")][0].digest // empty')"
universe_digest="$(printf '%s' "$release_assets" | jq -r '[.[] | select(.name == "historical_ohlcv_universe.json")][0].digest // empty')"
db_size="$(printf '%s' "$release_assets" | jq -r '[.[] | select(.name == "historical_ohlcv.sqlite")][0].size // empty')"
universe_size="$(printf '%s' "$release_assets" | jq -r '[.[] | select(.name == "historical_ohlcv_universe.json")][0].size // empty')"
db_updated="$(printf '%s' "$release_assets" | jq -r '[.[] | select(.name == "historical_ohlcv.sqlite")][0].updated_at // empty')"
universe_updated="$(printf '%s' "$release_assets" | jq -r '[.[] | select(.name == "historical_ohlcv_universe.json")][0].updated_at // empty')"

if [[ "$db_digest" =~ ^sha256:[0-9a-fA-F]{64}$ ]] &&
   [[ "$universe_digest" =~ ^sha256:[0-9a-fA-F]{64}$ ]] &&
   [[ "$db_size" =~ ^[0-9]+$ ]] && (( db_size <= MAX_RELEASE_ASSET_BYTES )) &&
   [[ "$universe_size" =~ ^[0-9]+$ ]] && (( universe_size <= MAX_RELEASE_ASSET_BYTES )) &&
   [[ -n "$db_updated" && -n "$universe_updated" ]]; then
  db_epoch="$(date -d "$db_updated" +%s)"
  universe_epoch="$(date -d "$universe_updated" +%s)"
  skew=$((db_epoch - universe_epoch))
  skew=${skew#-}
  if (( skew <= 120 )); then
    gh release download "$tag" --repo "$repo" --pattern historical_ohlcv.sqlite --dir "$tmp_dir"
    gh release download "$tag" --repo "$repo" --pattern historical_ohlcv_universe.json --dir "$tmp_dir"
    local_db_digest="$(sha256sum "$tmp_dir/historical_ohlcv.sqlite" | awk '{print $1}')"
    local_universe_digest="$(sha256sum "$tmp_dir/historical_ohlcv_universe.json" | awk '{print $1}')"
    if [[ "$local_db_digest" == "${db_digest#sha256:}" ]] &&
       [[ "$local_universe_digest" == "${universe_digest#sha256:}" ]] &&
       [[ "$(stat -c '%s' "$tmp_dir/historical_ohlcv.sqlite")" == "$db_size" ]] &&
       [[ "$(stat -c '%s' "$tmp_dir/historical_ohlcv_universe.json")" == "$universe_size" ]]; then
      python ohlcv_release_bundle.py restore-files \
        --source-db "$tmp_dir/historical_ohlcv.sqlite" \
        --source-universe "$tmp_dir/historical_ohlcv_universe.json" \
        --db "$db_path" --universe "$universe_path"
      echo "OHLCV_RELEASE_RESTORE=PASS | legacy-pair=remote-digest-verified"
      exit 0
    fi
    echo "::warning::Legacy-Download-Digest stimmt nicht mit Release-Metadaten überein."
  else
    echo "::warning::Legacy-Assets liegen mehr als 120 Sekunden auseinander; Legacy-Fallback gesperrt."
  fi
else
  echo "::warning::Kein vollständiges, remote-digest-verifizierbares Legacy-Asset-Paar vorhanden."
fi

echo "::error::Kein gültiger OHLCV-Bundle-Snapshot und kein verifiziertes Legacy-Paar verfügbar."
exit 1
