"""
Builds a single rolled-up QC summary across the last N days (default 7),
by pooling every day's raw A/B readings per sensor into one regression
instead of treating each day separately. This is what the dashboard's
"Last 7 days" view reads.

Sensor metadata (last_seen, rssi, channel_flags) is inherently a
*current* snapshot from PurpleAir — there's no historical metadata API —
so it's taken from the most recent day's sensors_meta.csv in the window,
same as the "last day" view. Offline status is a live property, not a
per-day one; this rollup doesn't try to pretend otherwise.

Usage:
    python rollup.py [--days 7] [--end 2026-09-13]
"""
from __future__ import annotations

import json
import argparse
import logging
from pathlib import Path
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from analyze import pair_stats, classify, attach_nearest_neighbors, SIZE_FRACTIONS, PRIMARY_FRACTION, CONFIG

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("rollup")

ROOT = Path(__file__).resolve().parent.parent
TH = CONFIG["thresholds"]


def rollup(days: int = 7, end_date: str | None = None):
    now = datetime.now(timezone.utc)
    if end_date is None:
        end_date = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")
    window_dates = [(end_dt - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days)]
    window_dates.sort()

    existing_dirs = [d for d in window_dates if (ROOT / CONFIG["output"]["raw_dir"] / d).exists()]
    if not existing_dirs:
        raise FileNotFoundError(
            f"No raw data found for any date in {window_dates}. Run fetch_data.py / backfill.py first."
        )
    log.info("Rolling up %d/%d available day(s): %s", len(existing_dirs), len(window_dates), existing_dirs)

    # Freshest metadata snapshot in the window (last_seen etc. are current-state anyway)
    latest_dir = ROOT / CONFIG["output"]["raw_dir"] / existing_dirs[-1]
    meta = pd.read_csv(latest_dir / "sensors_meta.csv")

    offline_cutoff_s = TH["offline_hours"] * 3600
    expected_slots_per_day = int(24 * 60 / CONFIG["history"]["average_minutes"])
    expected_slots_total = expected_slots_per_day * len(window_dates)

    results = []
    for _, row in meta.iterrows():
        idx = int(row["sensor_index"])
        name = str(row.get("name", f"sensor_{idx}"))

        last_seen = row.get("last_seen")
        is_offline = True
        if pd.notna(last_seen):
            age_s = now.timestamp() - float(last_seen)
            is_offline = age_s > offline_cutoff_s

        # Pool every available day's history for this sensor into one frame
        frames = []
        for d in existing_dirs:
            fpath = ROOT / CONFIG["output"]["raw_dir"] / d / f"sensor_{idx}.csv"
            if fpath.exists():
                frames.append(pd.read_csv(fpath))
        hist = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
        if not hist.empty and "time_stamp" in hist.columns:
            hist = hist.sort_values("time_stamp")

        completeness = 100.0 * len(hist) / expected_slots_total if expected_slots_total else 0.0

        stats_by_fraction = {frac: pair_stats(np.array([]), np.array([])) for frac in SIZE_FRACTIONS}
        timeseries = {"timestamps": []}
        for frac in SIZE_FRACTIONS:
            timeseries[f"{frac}_a"] = []
            timeseries[f"{frac}_b"] = []

        if not hist.empty:
            for frac, (col_a, col_b) in SIZE_FRACTIONS.items():
                if col_a in hist.columns and col_b in hist.columns:
                    paired = hist.dropna(subset=[col_a, col_b])
                    a = paired[col_a].to_numpy(dtype=float)
                    b = paired[col_b].to_numpy(dtype=float)
                    stats_by_fraction[frac] = pair_stats(a, b)
            if "time_stamp" in hist.columns:
                timeseries["timestamps"] = hist["time_stamp"].tolist()
                for frac, (col_a, col_b) in SIZE_FRACTIONS.items():
                    if col_a in hist.columns and col_b in hist.columns:
                        timeseries[f"{frac}_a"] = hist[col_a].tolist()
                        timeseries[f"{frac}_b"] = hist[col_b].tolist()

        primary_stats = stats_by_fraction[PRIMARY_FRACTION]
        status, reasons = classify(primary_stats, is_offline, row.get("channel_flags"), completeness)

        mean_by_fraction = {}
        for frac in SIZE_FRACTIONS:
            fa, fb = timeseries.get(f"{frac}_a"), timeseries.get(f"{frac}_b")
            mean_by_fraction[frac] = {"mean": None, "mean_a": None, "mean_b": None}
            if fa:
                arr_a = np.array(fa, dtype=float)
                arr_a = arr_a[~np.isnan(arr_a)]
                if len(arr_a):
                    mean_by_fraction[frac]["mean_a"] = round(float(np.mean(arr_a)), 2)
            if fb:
                arr_b = np.array(fb, dtype=float)
                arr_b = arr_b[~np.isnan(arr_b)]
                if len(arr_b):
                    mean_by_fraction[frac]["mean_b"] = round(float(np.mean(arr_b)), 2)
            if fa and fb:
                combined = np.array(fa + fb, dtype=float)
                combined = combined[~np.isnan(combined)]
                if len(combined):
                    mean_by_fraction[frac]["mean"] = round(float(np.mean(combined)), 2)

        results.append({
            "sensor_index": idx,
            "name": name,
            "latitude": row.get("latitude"),
            "longitude": row.get("longitude"),
            "last_seen": int(last_seen) if pd.notna(last_seen) else None,
            "date_created": int(row["date_created"]) if pd.notna(row.get("date_created")) else None,
            "rssi": row.get("rssi"),
            "channel_state": row.get("channel_state"),
            "channel_flags": row.get("channel_flags"),
            "completeness_pct": round(completeness, 1),
            "mean_by_fraction": mean_by_fraction,
            "stats": primary_stats,
            "stats_by_fraction": stats_by_fraction,
            "status": status,
            "reasons": reasons,
            "timeseries": timeseries,
        })

    attach_nearest_neighbors(results, k=2)

    summary = {
        "window_start": existing_dirs[0],
        "window_end": existing_dirs[-1],
        "days_included": existing_dirs,
        "generated_at": now.isoformat(),
        "n_sensors": len(results),
        "counts": {
            s: sum(1 for r in results if r["status"] == s)
            for s in ["OK", "WARNING", "FAULTY", "OFFLINE"]
        },
        "sensors": results,
    }

    site_data_dir = ROOT / CONFIG["output"]["site_data_dir"]
    site_data_dir.mkdir(parents=True, exist_ok=True)
    (site_data_dir / "last7.json").write_text(json.dumps(summary, default=str))

    log.info("Rollup done (%s -> %s): %s", existing_dirs[0], existing_dirs[-1], summary["counts"])
    return summary


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--end", type=str, default=None, help="YYYY-MM-DD, default: yesterday UTC")
    args = ap.parse_args()
    rollup(days=args.days, end_date=args.end)
