"""
Downloads one day of data for every sensor in the configured bounding box
and saves it to data/raw/<date>/.

Usage:
    PURPLEAIR_API_KEY=xxxx python fetch_data.py [YYYY-MM-DD]

If no date is given, defaults to yesterday (UTC) — the safest choice since
"today" is still accumulating and PurpleAir's history endpoint back-fills
with a short delay.
"""
from __future__ import annotations

import sys
import json
import logging
from pathlib import Path

import pandas as pd

from purpleair_client import get_sensors_in_bbox, get_sensor_history, day_bounds_utc

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("fetch_data")

ROOT = Path(__file__).resolve().parent.parent
CONFIG = json.loads((ROOT / "config.json").read_text())


def main(date_str: str | None = None):
    start_ts, end_ts = day_bounds_utc(date_str)
    resolved_date = date_str or pd.Timestamp(start_ts, unit="s").strftime("%Y-%m-%d")
    log.info("Fetching data for %s (UTC %s -> %s)", resolved_date, start_ts, end_ts)

    out_dir = ROOT / CONFIG["output"]["raw_dir"] / resolved_date
    out_dir.mkdir(parents=True, exist_ok=True)

    max_age_seconds = CONFIG.get("sensor_list_max_age_days", 180) * 86400
    sensors = get_sensors_in_bbox(CONFIG["bbox"], max_age=max_age_seconds)
    if not sensors:
        log.warning("No sensors found in bounding box — check config.json bbox.")
        return

    # Save the sensor metadata snapshot (gives us last_seen, rssi, channel_flags etc.)
    meta_df = pd.DataFrame(sensors)
    meta_df.to_csv(out_dir / "sensors_meta.csv", index=False)
    log.info("Saved metadata for %d sensors -> %s", len(sensors), out_dir / "sensors_meta.csv")

    avg_min = CONFIG["history"]["average_minutes"]
    ok, failed = 0, []
    for s in sensors:
        idx = s["sensor_index"]
        name = s.get("name", f"sensor_{idx}")
        try:
            rows = get_sensor_history(idx, start_ts, end_ts, average_minutes=avg_min)
            if not rows:
                log.warning("No history rows for sensor %s (%s)", idx, name)
                continue
            df = pd.DataFrame(rows)
            # PurpleAir's JSON history endpoint returns time_stamp as raw
            # unix seconds. Add a human-readable UTC column alongside it —
            # time_stamp (numeric) stays first for the dashboard's charts,
            # datetime_utc (readable) is for you to eyeball the file.
            if "time_stamp" in df.columns:
                df.insert(1, "datetime_utc", pd.to_datetime(df["time_stamp"], unit="s", utc=True))
            df.to_csv(out_dir / f"sensor_{idx}.csv", index=False)
            ok += 1
        except Exception as exc:
            log.error("Failed to fetch history for sensor %s (%s): %s", idx, name, exc)
            failed.append(idx)

    log.info("Done. %d/%d sensors fetched successfully. Failures: %s", ok, len(sensors), failed)


if __name__ == "__main__":
    date_arg = sys.argv[1] if len(sys.argv) > 1 else None
    main(date_arg)
