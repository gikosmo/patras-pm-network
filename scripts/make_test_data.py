"""Generates synthetic raw data to test analyze.py without needing a real API key."""
import json
import numpy as np
import pandas as pd
from pathlib import Path
from datetime import datetime, timezone, timedelta

ROOT = Path(__file__).resolve().parent.parent
DATE = "2026-09-13"
raw_dir = ROOT / "data" / "raw" / DATE
raw_dir.mkdir(parents=True, exist_ok=True)

rng = np.random.default_rng(42)
now = datetime.now(timezone.utc)

n = 144  # 10-min slots in a day
t0 = int(datetime.strptime(DATE, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())
timestamps = [t0 + i * 600 for i in range(n)]

# Underlying "true" PM2.5 signal for the day (diurnal-ish pattern + noise)
true_pm = 8 + 4 * np.sin(np.linspace(0, 2 * np.pi, n)) + rng.normal(0, 1.0, n)
true_pm = np.clip(true_pm, 0, None)

sensors_meta = []

def write_hist(idx, a, b, ts=timestamps):
    df = pd.DataFrame({"time_stamp": ts, "pm2.5_atm_a": a, "pm2.5_atm_b": b})
    df.to_csv(raw_dir / f"sensor_{idx}.csv", index=False)

# 1. Healthy sensor: A and B track each other closely
noise_a = rng.normal(0, 0.4, n)
noise_b = rng.normal(0, 0.4, n)
write_hist(1, true_pm + noise_a, true_pm + noise_b)
sensors_meta.append(dict(sensor_index=1, name="Patras-Center-01", latitude=38.2466, longitude=21.7346,
                          last_seen=int(now.timestamp()) - 300, rssi=-55, channel_state=3, channel_flags=0))

# 2. Drifting sensor: B reads systematically ~40% high (fouled sensor / drift) -> WARNING/FAULTY via slope
write_hist(2, true_pm + rng.normal(0, 0.4, n), 1.4 * true_pm + rng.normal(0, 0.6, n) + 2)
sensors_meta.append(dict(sensor_index=2, name="Patras-Rio-02", latitude=38.2900, longitude=21.7890,
                          last_seen=int(now.timestamp()) - 600, rssi=-60, channel_state=3, channel_flags=0))

# 3. Faulty sensor: B is basically noise, uncorrelated with A (dead/clogged channel)
write_hist(3, true_pm + rng.normal(0, 0.4, n), rng.uniform(0, 30, n))
sensors_meta.append(dict(sensor_index=3, name="Patras-Agyia-03", latitude=38.2200, longitude=21.7500,
                          last_seen=int(now.timestamp()) - 900, rssi=-70, channel_state=3, channel_flags=2))

# 4. Offline sensor: last_seen is 10 hours ago, no history rows for "today"
sensors_meta.append(dict(sensor_index=4, name="Patras-Port-04", latitude=38.2500, longitude=21.7300,
                          last_seen=int(now.timestamp()) - 36000, rssi=None, channel_state=3, channel_flags=0))
# (no CSV written -> simulates no data returned by history endpoint)

# 5. Sparse data sensor: only 10 points today (intermittent connectivity), otherwise fine
idx_sparse = rng.choice(n, size=10, replace=False)
idx_sparse.sort()
write_hist(5, true_pm[idx_sparse] + rng.normal(0, 0.4, 10), true_pm[idx_sparse] + rng.normal(0, 0.4, 10),
           ts=[timestamps[i] for i in idx_sparse])
sensors_meta.append(dict(sensor_index=5, name="Patras-Akti-05", latitude=38.2350, longitude=21.7200,
                          last_seen=int(now.timestamp()) - 400, rssi=-80, channel_state=3, channel_flags=0))

meta_df = pd.DataFrame(sensors_meta)
meta_df.to_csv(raw_dir / "sensors_meta.csv", index=False)
print(f"Wrote synthetic data for {len(sensors_meta)} sensors to {raw_dir}")
print(meta_df[["sensor_index", "name", "last_seen"]])
