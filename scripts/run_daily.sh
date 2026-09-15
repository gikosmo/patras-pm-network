#!/usr/bin/env bash
# Run this daily via cron on your own machine/server as an alternative to
# GitHub Actions. Example crontab entry (runs 04:00 every day):
#   0 4 * * *  PURPLEAIR_API_KEY=xxxx /path/to/purpleair_agent/scripts/run_daily.sh >> /path/to/purpleair_agent/logs/daily.log 2>&1
set -euo pipefail
cd "$(dirname "$0")"

if [ -z "${PURPLEAIR_API_KEY:-}" ]; then
  echo "ERROR: PURPLEAIR_API_KEY is not set." >&2
  exit 1
fi

python3 fetch_data.py
python3 analyze.py

echo "Done. Serve the dashboard with: python3 -m http.server 8000 --directory ../site"
