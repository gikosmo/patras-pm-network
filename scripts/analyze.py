"""
Reads one day of raw sensor data and computes, per sensor:
  - Channel A vs Channel B agreement stats (slope, intercept, R2, RMSE, MAE,
    mean bias, % of readings within 20% of each other)
  - Data completeness (how many 10-min slots actually reported, out of 144/day)
  - Offline status (based on last_seen from the metadata snapshot)
  - PurpleAir's own channel_flags / channel_state (0=normal / A or B downgraded)
  - An overall status: OK / WARNING / FAULTY / OFFLINE

Usage:
    python analyze.py [YYYY-MM-DD]

Writes data/results/<date>/summary.json and site/data/<date>.json
(the latter is what the dashboard reads), plus updates site/data/latest.json
and site/data/index.json (list of available dates).
"""
from __future__ import annotations

import sys
import json
import logging
from pathlib import Path
from datetime import datetime, timezone

import numpy as np
import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("analyze")

ROOT = Path(__file__).resolve().parent.parent
CONFIG = json.loads((ROOT / "config.json").read_text())
TH = CONFIG["thresholds"]

# PurpleAir's cf_1 (correction factor 1) channel fields, per size fraction.
# PM2.5 is the primary fraction used for overall status classification
# (that's what the health-based thresholds in config.json are tuned for);
# PM1 and PM10 are computed and shown alongside for extra diagnostic value.
SIZE_FRACTIONS = {
    "pm1_0": ("pm1.0_cf_1_a", "pm1.0_cf_1_b"),
    "pm2_5": ("pm2.5_cf_1_a", "pm2.5_cf_1_b"),
    "pm10_0": ("pm10.0_cf_1_a", "pm10.0_cf_1_b"),
}
PRIMARY_FRACTION = "pm2_5"


def haversine_km(lat1, lon1, lat2, lon2) -> float:
    """Great-circle distance between two lat/lon points, in km."""
    r = 6371.0
    p1, p2 = np.radians(lat1), np.radians(lat2)
    dphi = np.radians(lat2 - lat1)
    dlmb = np.radians(lon2 - lon1)
    a = np.sin(dphi / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dlmb / 2) ** 2
    return float(2 * r * np.arcsin(np.sqrt(a)))


def attach_nearest_neighbors(results: list[dict], k: int = 2) -> None:
    """
    Mutates each result dict in place, adding a 'nearest_neighbors' list of
    the k geographically closest OTHER sensors, purely by straight-line
    distance. This does NOT filter by the neighbor's own QC status — a
    sensor that is itself WARNING/FAULTY/OFFLINE is still eligible if it's
    genuinely the closest. Each neighbor entry includes its own status so
    the dashboard can show whether you're comparing against a healthy
    reference sensor or another flagged one.
    """
    def has_location(r) -> bool:
        lat, lon = r.get("latitude"), r.get("longitude")
        if lat in (None, "") or lon in (None, ""):
            return False
        try:
            return not (pd.isna(lat) or pd.isna(lon))
        except (TypeError, ValueError):
            return True

    located = [r for r in results if has_location(r)]
    by_index = {r["sensor_index"]: r for r in located}

    for r in results:
        r["nearest_neighbors"] = []
        if r["sensor_index"] not in by_index:
            continue
        dists = []
        for idx, other in by_index.items():
            if idx == r["sensor_index"]:
                continue
            d = haversine_km(r["latitude"], r["longitude"], other["latitude"], other["longitude"])
            dists.append((d, other))
        dists.sort(key=lambda x: x[0])
        r["nearest_neighbors"] = [
            {
                "sensor_index": o["sensor_index"],
                "name": o["name"],
                "distance_km": round(d, 2),
                "status": o.get("status"),
            }
            for d, o in dists[:k]
        ]


def pair_stats(a: np.ndarray, b: np.ndarray) -> dict:
    """Regression + agreement stats between two simultaneous channel readings."""
    n = len(a)
    if n < 2:
        return {"n_paired": n, "slope": None, "intercept": None, "r2": None,
                "rmse": None, "mae": None, "mean_bias": None, "pct_within_20pct": None}

    # Ordinary least squares b ~ slope*a + intercept (A is the reference axis)
    slope, intercept = np.polyfit(a, b, 1)
    pred = slope * a + intercept
    ss_res = np.sum((b - pred) ** 2)
    ss_tot = np.sum((b - np.mean(b)) ** 2)
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else (1.0 if ss_res == 0 else 0.0)

    rmse = float(np.sqrt(np.mean((a - b) ** 2)))
    mae = float(np.mean(np.abs(a - b)))
    mean_bias = float(np.mean(b - a))

    # % of paired points where the two channels agree within 20% of their mean
    denom = np.where((a + b) / 2 == 0, np.nan, (a + b) / 2)
    pct_diff = np.abs(a - b) / denom
    pct_within_20 = float(np.nanmean(pct_diff <= 0.20) * 100)

    return {
        "n_paired": int(n),
        "slope": round(float(slope), 4),
        "intercept": round(float(intercept), 4),
        "r2": round(float(r2), 4),
        "rmse": round(rmse, 4),
        "mae": round(mae, 4),
        "mean_bias": round(mean_bias, 4),
        "pct_within_20pct": round(pct_within_20, 1),
    }


def classify(stats: dict, is_offline: bool, channel_flags, completeness: float) -> tuple[str, list[str]]:
    """Return (status, reasons[]) — status in OK / WARNING / FAULTY / OFFLINE."""
    reasons = []

    if is_offline:
        return "OFFLINE", ["No data received within offline threshold (WiFi/power likely down)"]

    try:
        channel_flags = int(channel_flags)
    except (TypeError, ValueError):
        channel_flags = 0
    if channel_flags in (1, 2, 3):
        label = {1: "Channel A", 2: "Channel B", 3: "Both channels"}[channel_flags]
        reasons.append(f"PurpleAir flags {label} as downgraded")

    if stats["n_paired"] < TH["min_paired_points"]:
        reasons.append(f"Too few paired A/B readings today ({stats['n_paired']}) to trust QC stats")
        # Not enough data to say much beyond "insufficient data"
        status = "WARNING" if not reasons[:-1] else "FAULTY"
        return status, reasons

    r2, slope = stats["r2"], stats["slope"]
    fault = False
    warn = False

    if r2 is not None and r2 < TH["r2_fault"]:
        reasons.append(f"R²={r2} between A and B is very low (< {TH['r2_fault']})")
        fault = True
    elif r2 is not None and r2 < TH["r2_warning"]:
        reasons.append(f"R²={r2} between A and B is degraded (< {TH['r2_warning']})")
        warn = True

    if slope is not None and (slope < TH["slope_fault_low"] or slope > TH["slope_fault_high"]):
        reasons.append(f"Slope={slope} between A and B is far from 1")
        fault = True
    elif slope is not None and (slope < TH["slope_warning_low"] or slope > TH["slope_warning_high"]):
        reasons.append(f"Slope={slope} between A and B is drifting from 1")
        warn = True

    if channel_flags in (1, 2, 3):
        fault = True

    if completeness < 50:
        reasons.append(f"Only {completeness:.0f}% of expected readings received today")
        warn = True

    if fault:
        return "FAULTY", reasons
    if warn:
        return "WARNING", reasons
    return "OK", ["A and B channels agree well"]


def analyze_date(date_str: str):
    raw_dir = ROOT / CONFIG["output"]["raw_dir"] / date_str
    if not raw_dir.exists():
        raise FileNotFoundError(f"No raw data found for {date_str} at {raw_dir}. Run fetch_data.py first.")

    meta = pd.read_csv(raw_dir / "sensors_meta.csv")
    now = datetime.now(timezone.utc)
    offline_cutoff_s = TH["offline_hours"] * 3600
    expected_slots = int(24 * 60 / CONFIG["history"]["average_minutes"])

    results = []
    for _, row in meta.iterrows():
        idx = int(row["sensor_index"])
        name = str(row.get("name", f"sensor_{idx}"))
        hist_path = raw_dir / f"sensor_{idx}.csv"

        last_seen = row.get("last_seen")
        is_offline = False
        if pd.notna(last_seen):
            age_s = now.timestamp() - float(last_seen)
            is_offline = age_s > offline_cutoff_s
        else:
            is_offline = True

        if hist_path.exists():
            hist = pd.read_csv(hist_path)
            if not hist.empty and "time_stamp" in hist.columns:
                hist = hist.sort_values("time_stamp")
        else:
            hist = pd.DataFrame()

        completeness = 0.0
        stats_by_fraction = {frac: pair_stats(np.array([]), np.array([])) for frac in SIZE_FRACTIONS}
        timeseries = {"timestamps": []}
        for frac in SIZE_FRACTIONS:
            timeseries[f"{frac}_a"] = []
            timeseries[f"{frac}_b"] = []

        if not hist.empty:
            expected_cols_present = all(
                col in hist.columns for pair in SIZE_FRACTIONS.values() for col in pair
            )
            # completeness is based on row count regardless of which fractions are present
            completeness = 100.0 * len(hist) / expected_slots

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
        "date": date_str,
        "generated_at": now.isoformat(),
        "n_sensors": len(results),
        "counts": {
            s: sum(1 for r in results if r["status"] == s)
            for s in ["OK", "WARNING", "FAULTY", "OFFLINE"]
        },
        "sensors": results,
    }

    results_dir = ROOT / CONFIG["output"]["results_dir"] / date_str
    results_dir.mkdir(parents=True, exist_ok=True)
    (results_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str))

    site_data_dir = ROOT / CONFIG["output"]["site_data_dir"]
    site_data_dir.mkdir(parents=True, exist_ok=True)
    (site_data_dir / f"{date_str}.json").write_text(json.dumps(summary, default=str))
    (site_data_dir / "latest.json").write_text(json.dumps(summary, default=str))

    index_path = site_data_dir / "index.json"
    dates = []
    if index_path.exists():
        dates = json.loads(index_path.read_text())
    if date_str not in dates:
        dates.append(date_str)
        dates.sort()
    index_path.write_text(json.dumps(dates))

    log.info("Analyzed %s: %s", date_str, summary["counts"])
    return summary


if __name__ == "__main__":
    if len(sys.argv) > 1:
        target_date = sys.argv[1]
    else:
        from datetime import timedelta
        target_date = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    analyze_date(target_date)
