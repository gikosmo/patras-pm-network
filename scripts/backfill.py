"""
Backfills the QC pipeline over the last N days (default 7), so the
dashboard has more than a single day of history to look at.

Usage:
    PURPLEAIR_API_KEY=xxxx python backfill.py            # last 7 days
    PURPLEAIR_API_KEY=xxxx python backfill.py --days 14  # last 14 days
    PURPLEAIR_API_KEY=xxxx python backfill.py --start 2026-06-29 --end 2026-09-12

Runs fetch_data.py then analyze.py for each day in sequence, with a short
pause between days to stay well within PurpleAir's rate limits.
"""
from __future__ import annotations

import sys
import time
import argparse
import logging
from datetime import datetime, timedelta, timezone

import fetch_data
import analyze
import rollup

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("backfill")


def daterange(start: str, end: str):
    d0 = datetime.strptime(start, "%Y-%m-%d")
    d1 = datetime.strptime(end, "%Y-%m-%d")
    d = d0
    while d <= d1:
        yield d.strftime("%Y-%m-%d")
        d += timedelta(days=1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7, help="How many past days to backfill (default 7)")
    ap.add_argument("--start", type=str, default=None, help="YYYY-MM-DD start date (overrides --days)")
    ap.add_argument("--end", type=str, default=None, help="YYYY-MM-DD end date (default: yesterday UTC)")
    ap.add_argument("--sleep", type=float, default=3.0, help="Seconds to pause between days")
    args = ap.parse_args()

    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    end = args.end or yesterday
    if args.start:
        start = args.start
    else:
        start_dt = datetime.strptime(end, "%Y-%m-%d") - timedelta(days=args.days - 1)
        start = start_dt.strftime("%Y-%m-%d")

    dates = list(daterange(start, end))
    log.info("Backfilling %d day(s): %s -> %s", len(dates), dates[0], dates[-1])

    ok, failed = 0, []
    for i, d in enumerate(dates):
        log.info("--- [%d/%d] %s ---", i + 1, len(dates), d)
        try:
            fetch_data.main(d)
            analyze.analyze_date(d)
            ok += 1
        except Exception as exc:
            log.error("Failed on %s: %s", d, exc)
            failed.append(d)
        if i < len(dates) - 1:
            time.sleep(args.sleep)

    log.info("Backfill done. %d/%d days succeeded. Failures: %s", ok, len(dates), failed)

    if ok:
        log.info("Building rolled-up last-%d-days summary for the dashboard...", len(dates))
        rollup.rollup(days=len(dates), end_date=end)


if __name__ == "__main__":
    main()
