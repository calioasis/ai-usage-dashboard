#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

python3 collect_usage.py

if git diff --quiet -- data/usage-data.json && git diff --cached --quiet -- data/usage-data.json; then
  echo "No change in usage data, nothing to push."
  exit 0
fi

git add data/usage-data.json
git commit -m "Refresh usage data ($(date -u +%Y-%m-%dT%H:%MZ))"
git push
