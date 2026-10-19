#!/usr/bin/env bash
# Fail when a commit in a range changes product code without a CHANGELOG.md
# entry. A product commit is covered when it, or a later commit in the range
# built on top of it, changes CHANGELOG.md; a changelog commit that came before
# it never covers it. A genuinely internal commit (a refactor, tooling) opts
# out with a `Changelog: none` trailer.
#
# Usage: scripts/check-changelog.sh [<range>]
# <range> is git rev-list arguments, e.g. `main..feature` or
# `HEAD --not --remotes`. With none, checks what this branch contributes: its
# commits not yet upstream, or not on any remote when it has no upstream.
set -euo pipefail

repo_root="$(git rev-parse --show-toplevel)"
cd "$repo_root"

if [[ -n "${1:-}" ]]; then
    range="$1"
else
    # Commits not yet upstream, or not on any remote when the branch has no upstream.
    upstream="$(git rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null || true)"
    if [[ -n "$upstream" ]]; then
        range="${upstream}..HEAD"
    else
        range="HEAD --not --remotes"
    fi
fi
read -ra revs <<< "$range"
echo "  range: $range"

product_paths=(':(glob)src/**')

# --full-history: without it, a merge that matches one parent hides the
# commits on its other side.
product_commits="$(git log --full-history --no-merges --format=%H "${revs[@]}" -- "${product_paths[@]}")"
if [[ -z "$product_commits" ]]; then
    echo "  no product code changed"
    exit 0
fi

changelog_commits="$(git log --full-history --no-merges --format=%H "${revs[@]}" -- CHANGELOG.md)"
changelog_merges="$(git log --full-history --merges --format=%H "${revs[@]}" -- CHANGELOG.md)"

fail() {
    echo "  ! $1" >&2
    exit 2
}

# A merge counts only when it writes an entry of its own, i.e. its CHANGELOG.md
# differs from every parent's; one that just brings in another branch's entry
# does not document this branch's commits.
while read -r merge; do
    [[ -n "$merge" ]] || continue
    parents="$(git rev-parse "$merge^@")"
    [[ -n "$parents" ]] || fail "could not read the parents of merge $merge"
    compared=0
    differing=0
    while read -r parent; do
        compared=$((compared + 1))
        status=0
        git diff --quiet "$parent" "$merge" -- CHANGELOG.md || status=$?
        case "$status" in
            0) break ;;
            1) differing=$((differing + 1)) ;;
            *) fail "could not compare CHANGELOG.md between $parent and $merge" ;;
        esac
    done <<< "$parents"
    if [[ "$compared" -gt 0 && "$differing" -eq "$compared" ]]; then
        changelog_commits+=$'\n'"$merge"
    fi
done <<< "$changelog_merges"

missing=()
while read -r sha; do
    trailers="$(git log -1 --format='%(trailers:key=Changelog,valueonly)' "$sha")"
    trailer="${trailers%%$'\n'*}"
    trailer="${trailer//[[:space:]]/}"
    if [[ "${trailer,,}" == "none" ]]; then
        continue
    fi
    covered=0
    while read -r entry; do
        [[ -n "$entry" ]] || continue
        status=0
        git merge-base --is-ancestor "$sha" "$entry" || status=$?
        case "$status" in
            0) covered=1; break ;;
            1) ;;
            *) fail "could not check whether $entry is built on $sha" ;;
        esac
    done <<< "$changelog_commits"
    if [[ "$covered" -eq 0 ]]; then
        missing+=("$sha")
    fi
done <<< "$product_commits"

if [[ ${#missing[@]} -eq 0 ]]; then
    echo "  every product-code commit has a CHANGELOG.md entry or 'Changelog: none'"
    exit 0
fi

echo "  ! product code changed with no CHANGELOG.md entry in or after these commits:" >&2
for sha in "${missing[@]}"; do
    summary="$(git log -1 --format='%h %s' "$sha")"
    echo "      $summary" >&2
done
echo "  Add a CHANGELOG.md entry, or, for a change with no user-visible effect," >&2
echo "  add the trailer 'Changelog: none' to each commit listed." >&2
exit 1
