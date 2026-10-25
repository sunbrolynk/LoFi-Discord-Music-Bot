#!/usr/bin/env bash
# Audits every lock and fails only on advisories a change introduces; ones already on the base branch
# are listed, not blocking. PyNaCl's two advisories stay ignored for the reason in requirements.in.
# Base ref: LOFI_AUDIT_BASE, default origin/dev.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

audit=.venv/bin/pip-audit
[[ -x "$audit" ]] || audit=pip-audit
base="${LOFI_AUDIT_BASE:-origin/dev}"
ignore=(--ignore-vuln PYSEC-2026-1448 --ignore-vuln PYSEC-2026-3002)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

# One "name==version id" line per advisory or unauditable package; a pip-audit crash fails the audit.
advisories() {
    rm -f "$tmp/out.json"
    "$audit" --strict --require-hashes --disable-pip "${ignore[@]}" -f json -o "$tmp/out.json" -r "$1" \
        >/dev/null 2>"$tmp/err" || true
    python3 -I - "$tmp/out.json" <<'PY' || { cat "$tmp/err" >&2; return 1; }
import json, sys
for dep in json.load(open(sys.argv[1]))["dependencies"]:
    if "skip_reason" in dep:
        print(f'{dep["name"]} skipped')
    for vuln in dep.get("vulns", []):
        print(f'{dep["name"]}=={dep["version"]} {vuln["id"]}')
PY
}

status=0
for lock in requirements.txt requirements-dev.txt; do
    echo "Auditing $lock against $base..."
    advisories "$lock" | sort -u > "$tmp/now"
    if git show "$base:$lock" > "$tmp/base.txt" 2>/dev/null; then
        advisories "$tmp/base.txt" | sort -u > "$tmp/was"
    else
        : > "$tmp/was"
    fi
    known=$(comm -12 "$tmp/now" "$tmp/was")
    new=$(comm -23 "$tmp/now" "$tmp/was")
    if [[ -n "$known" ]]; then
        echo "  already on $base, not blocking (update it on its own branch):"
        sed 's/^/    /' <<< "$known"
    fi
    if [[ -n "$new" ]]; then
        echo "  NEW advisories from this change:" >&2
        sed 's/^/    /' <<< "$new" >&2
        status=1
    else
        echo "  no new advisories"
    fi
done
exit $status
