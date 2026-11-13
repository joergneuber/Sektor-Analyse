#!/usr/bin/env bash
# Return every uploaded asset for a GitHub Release as one JSON array.
# The release object embeds assets but that list is not the paginated assets API.
set -euo pipefail

repo="${1:-${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}}"
tag="${2:-historical-ohlcv}"

release_json="$(gh api "repos/${repo}/releases/tags/${tag}")"
release_id="$(printf '%s' "$release_json" | jq -er '.id | select(type == "number")')"

# --paginate follows Link headers; --slurp collects each page. Flatten the
# array-of-pages and reject malformed responses rather than using a
# potentially truncated embedded .assets array.
pages_json="$(gh api --paginate --slurp "repos/${repo}/releases/${release_id}/assets?per_page=100")"
printf '%s' "$pages_json" |
  jq -ce '
    if type == "array" and all(.[]; type == "array") then
      [ .[][] | select(.state == "uploaded") ] as $assets
      | if all($assets[]; (.name | type == "string") and (.size | type == "number") and (.size >= 0))
           and ([$assets[].name] | length == (unique | length)) then
          $assets
        else
          error("GitHub release assets contain missing names/sizes or duplicate names")
        end
    else
      error("GitHub release asset pagination returned an unexpected JSON shape")
    end
  '
