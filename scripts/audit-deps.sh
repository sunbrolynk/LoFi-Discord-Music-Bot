#!/usr/bin/env bash
# Audits every lock for known vulnerabilities. PyNaCl's two advisories stay ignored for the reason in requirements.in.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

audit=.venv/bin/pip-audit
[[ -x "$audit" ]] || audit=pip-audit
ignore=(--ignore-vuln PYSEC-2026-1448 --ignore-vuln PYSEC-2026-3002)
for lock in requirements.txt requirements-dev.txt; do
    echo "Auditing $lock..."
    "$audit" --strict --require-hashes --disable-pip "${ignore[@]}" -r "$lock"
done
