#!/usr/bin/env bash
# Idempotent cleanup of old OHLCV release assets.
# The caller must supply two independently verified bundle names to retain.
set -euo pipefail

repo="${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"
tag="historical-ohlcv"
keep_current="${1:?current verified bundle name is required}"
keep_rollback="${2:?rollback verified bundle name is required}"
shift 2
targets=("$@")
max_attempts="${OHLCV_RELEASE_DELETE_MAX_ATTEMPTS:-3}"

if ! [[ "$max_attempts" =~ ^[1-9][0-9]*$ ]] || (( max_attempts > 5 )); then
  echo "::error::OHLCV_RELEASE_DELETE_MAX_ATTEMPTS must be an integer between 1 and 5."
  exit 2
fi
if [ "$keep_current" = "$keep_rollback" ]; then
  echo "::error::Current and rollback OHLCV bundle must be distinct."
  exit 2
fi
for protected in "$keep_current" "$keep_rollback"; do
  if ! [[ "$protected" =~ ^historical_ohlcv_bundle_[A-Za-z0-9_.-]+\.zip$ ]]; then
    echo "::error::Invalid protected OHLCV bundle name: ${protected}"
    exit 2
  fi
done
for target in "${targets[@]}"; do
  if ! [[ "$target" =~ ^historical_ohlcv_bundle_[A-Za-z0-9_.-]+\.zip$ ]] ||
     [ "$target" = "$keep_current" ] || [ "$target" = "$keep_rollback" ]; then
    echo "::error::Invalid or protected OHLCV cleanup candidate: ${target}"
    exit 2
  fi
done

fetch_assets() {
  local payload
  payload="$(bash scripts/get_gh_release_assets.sh "$repo" "$tag")" || return 1
  printf '%s' "$payload" | jq -ce 'if type == "array" then . else error("release assets response is not an array") end'
}

asset_exists() {
  local payload="$1"
  local name="$2"
  printf '%s' "$payload" | jq -e --arg name "$name" 'any(.[]; .name == $name)' >/dev/null
}

assert_kept_assets_present() {
  local payload="$1"
  local name
  for name in "$keep_current" "$keep_rollback"; do
    if ! asset_exists "$payload" "$name"; then
      echo "::error::Verified OHLCV rollback asset disappeared: ${name}"
      return 1
    fi
  done
}

delete_asset_idempotent() {
  local asset_name="$1"
  local attempt payload delete_rc

  for ((attempt = 1; attempt <= max_attempts; attempt++)); do
    if ! payload="$(fetch_assets)"; then
      echo "::warning::Could not refresh release asset state before deleting ${asset_name} (attempt ${attempt}/${max_attempts})."
      sleep 2
      continue
    fi

    if ! asset_exists "$payload" "$asset_name"; then
      echo "OHLCV_RELEASE_ASSET_ALREADY_ABSENT=${asset_name}"
      return 0
    fi

    if gh release delete-asset "$tag" "$asset_name" --yes; then
      delete_rc=0
    else
      delete_rc=$?
      echo "::warning::Delete request for ${asset_name} returned ${delete_rc}; remote state will be checked before retry."
    fi

    # A 5xx response is ambiguous: the server may have deleted the asset
    # before the response failed. Only the refreshed remote state is decisive.
    if payload="$(fetch_assets)"; then
      if ! asset_exists "$payload" "$asset_name"; then
        echo "OHLCV_RELEASE_ASSET_DELETE_CONFIRMED=${asset_name}|attempt=${attempt}|delete_rc=${delete_rc}"
        return 0
      fi
    else
      echo "::warning::Could not verify remote state after deleting ${asset_name}."
    fi

    if [ "$attempt" -lt "$max_attempts" ]; then
      sleep 2
    fi
  done

  echo "::error::OHLCV release asset remains present or its deletion could not be verified: ${asset_name}"
  return 1
}

assets="$(fetch_assets)"
assert_kept_assets_present "$assets"

# Only delete names selected from the caller's already verified asset snapshot.
# Assets appearing concurrently after that snapshot are never swept implicitly.

for asset_name in "${targets[@]}"; do
  delete_asset_idempotent "$asset_name"
done

for legacy_asset in historical_ohlcv.sqlite historical_ohlcv_universe.json; do
  if asset_exists "$assets" "$legacy_asset"; then
    delete_asset_idempotent "$legacy_asset"
  fi
done

# Final proof: both protected bundles still exist and every cleanup candidate
# is absent. A partial cleanup is safe to rerun because absent assets are success.
assets="$(fetch_assets)"
assert_kept_assets_present "$assets"
for asset_name in "${targets[@]}" historical_ohlcv.sqlite historical_ohlcv_universe.json; do
  if asset_exists "$assets" "$asset_name"; then
    echo "::error::OHLCV cleanup final verification failed; asset still exists: ${asset_name}"
    exit 1
  fi
done

echo "OHLCV-Release-Bereinigung PASS: zwei verifizierte Bundles erhalten; alle alten Bundle-/Legacy-Assets nachweislich entfernt."
