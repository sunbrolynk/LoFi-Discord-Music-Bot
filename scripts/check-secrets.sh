#!/usr/bin/env bash
# Scans every commit on every ref for secrets, or only the staged changes with --staged.
set -euo pipefail

# git commit -a and path-limited commits hand the hook a separate index; that index is what gets
# committed, so a staged scan must read it. Relative paths are relative to the hook's directory.
index="${GIT_INDEX_FILE:-}"
[[ -n "$index" && "$index" != /* ]] && index="$PWD/$index"
# Hooks export GIT_DIR, which makes the paths below resolve from the hook instead of the checkout.
unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE

repo_root="$(git -C "$(dirname "${BASH_SOURCE[0]}")" rev-parse --show-toplevel)"
# A worktree's .git points into the main clone, so mount that too, both at their real paths.
git_dir="$(git -C "$repo_root" rev-parse --path-format=absolute --git-common-dir)"
scope=(--log-opts="--all")
index_args=()
if [[ "${1:-}" == "--staged" ]]; then
    scope=(--pre-commit --staged)
    if [[ -n "$index" ]]; then
        index_args=(-e "GIT_INDEX_FILE=$index")
        # It normally lives in the git dir mounted below; anywhere else needs its own mount.
        case "$index" in
            "$git_dir"/* | "$repo_root"/*) ;;
            *) index_args+=(--mount "type=bind,source=$(dirname "$index"),target=$(dirname "$index"),readonly") ;;
        esac
    fi
fi

# gitleaks logs git errors and still exits 0 with nothing scanned, so any ERR line fails the scan.
log="$(mktemp)"
trap 'rm -f "$log"' EXIT
set +e
docker run --rm --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges \
  --mount "type=bind,source=$repo_root,target=$repo_root,readonly" \
  --mount "type=bind,source=$git_dir,target=$git_dir,readonly" \
  -e GIT_CONFIG_COUNT=1 \
  -e GIT_CONFIG_KEY_0=safe.directory \
  -e GIT_CONFIG_VALUE_0='*' \
  "${index_args[@]}" \
  zricethezav/gitleaks:v8.30.0@sha256:691af3c7c5a48b16f187ce3446d5f194838f91238f27270ed36eef6359a574d9 \
  git "$repo_root" "${scope[@]}" --redact=100 --no-banner --no-color 2>&1 | tee "$log"
status="${PIPESTATUS[0]}"
set -e
if [[ "$status" != 0 ]] || grep -q "ERR" "$log"; then
    echo "Secret scan found a leak or could not read the repository." >&2
    exit 1
fi
