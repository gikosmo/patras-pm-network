# Patras PM network — PurpleAir sensor QC

Downloads daily PM2.5 data for every PurpleAir sensor in the Patras network,
checks each sensor's two internal channels (A and B) against each other with
a linear regression (slope, intercept, R², RMSE, MAE), flags sensors whose
channels disagree or that have gone offline, and publishes the results as a
small static dashboard.

Every PurpleAir sensor has two independent laser particle counters (channel A
and channel B) measuring the same air. Healthy sensors: A ≈ B, so slope ≈ 1
and R² close to 1. A sensor with a fouled/failing laser, a hardware fault, or
firmware issue will show the two channels drifting apart — that's the signal
this pipeline is built to catch. Offline sensors are caught separately via
PurpleAir's `last_seen` timestamp.

## How it works

```
scripts/fetch_data.py   -> pulls sensor list + 10-min history from the
                            PurpleAir API for one UTC day, saves raw CSVs
                            to data/raw/<date>/
scripts/analyze.py      -> reads the raw CSVs, computes A-vs-B regression
                            stats per sensor, classifies each sensor as
                            OK / WARNING / FAULTY / OFFLINE, writes
                            data/results/<date>/summary.json and
                            site/data/<date>.json (+ latest.json, index.json)
site/index.html          -> static dashboard: map + status table + per-sensor
                            scatter (A vs B) and time-series charts. Reads
                            the JSON files above via fetch(), no backend needed.
```

## 1. Get a PurpleAir API key

Sign in at <https://develop.purpleair.com/>, go to the "Keys" tab, and
create a **read** key. Free for reasonable non-commercial use; PurpleAir may
ask about your use case.

## 2. Set your sensor network's bounding box

Edit `config.json` — the `bbox` block currently covers central Patras. Widen
or narrow it to match where your network's sensors actually are. You can
verify coverage by checking the sensor map at <https://map.purpleair.com/>
first.

Also review the `thresholds` block — the defaults (R² < 0.90 = warning,
< 0.70 = fault; slope outside 0.85–1.15 = warning, outside 0.5–2.0 = fault;
offline after 2 hours of silence) are reasonable starting points but you
should tune them against a few weeks of your own network's normal behavior.

## 3. Run it locally

```bash
cd purpleair_agent
pip install -r requirements.txt
export PURPLEAIR_API_KEY=your_read_key_here

python3 scripts/fetch_data.py        # defaults to yesterday (UTC)
python3 scripts/analyze.py           # same date

# view the dashboard
python3 -m http.server 8000 --directory site
# open http://localhost:8000
```

You can also run `python3 scripts/fetch_data.py 2026-09-10` for a specific
past date (subject to PurpleAir's history retention on your account tier).

There's a demo dataset already committed at `data/raw/2026-09-13` /
`site/data/2026-09-13.json` (synthetic — one healthy, one drifting, one
faulty, one offline, one sparse-data sensor) so you can see what the
dashboard looks like immediately, before wiring up a real key.

## 4. Automate it — two options

**Option A: your own server + cron** (simplest if you already have a
machine running). Use `scripts/run_daily.sh`:

```cron
0 4 * * *  PURPLEAIR_API_KEY=xxxx /path/to/purpleair_agent/scripts/run_daily.sh >> /path/to/purpleair_agent/logs/daily.log 2>&1
```

**Option B: GitHub Actions + GitHub Pages** (no server to maintain, and
gives you a public URL for free):

1. Push this folder to a GitHub repo.
2. Repo Settings → Secrets and variables → Actions → New repository secret:
   name `PURPLEAIR_API_KEY`, value = your key.
3. Repo Settings → Pages → Source: "GitHub Actions".
4. The included workflow (`.github/workflows/daily.yml`) runs every day at
   04:00 UTC, fetches + analyzes yesterday's data, commits the results, and
   deploys `site/` to GitHub Pages. You can also trigger it manually from
   the Actions tab ("Run workflow") to backfill a specific date.
5. Your dashboard will be live at
   `https://<your-username>.github.io/<repo-name>/`.

## 5. Reading the dashboard

- **Map**: sensors colored by status (green=OK, amber=WARNING, red=FAULTY,
  gray=OFFLINE). Click a marker for detail.
- **Table**: sortable-by-eye, worst-first. Click a row for the per-sensor
  scatter plot (A vs B, with the 1:1 reference line) and time-series chart.
- **Date selector**: switches between any date that has been run.

## Extending this

- **Alerting**: `analyze.py`'s `summary["counts"]` and each sensor's
  `status`/`reasons` are easy to pipe into a Slack webhook, email, or
  Telegram bot — add a step at the end of `analyze_date()` or a new script
  that reads `site/data/latest.json`.
- **Longer QC history / trend of R² over time**: `site/data/<date>.json` is
  kept for every date that's been run, so a small addition to `analyze.py`
  could roll these up into a per-sensor trend file for the dashboard to plot
  (e.g. "this sensor's R² has been declining for two weeks" — often an
  earlier warning than a single bad day).
- **Different averaging window**: `config.json → history.average_minutes`
  (PurpleAir supports 0/10/30/60/360/1440-minute averages).
- **Indoor sensors / different pollutants**: the PurpleAir API also exposes
  PM1.0, PM10, and other fields — add them to `HISTORY_FIELDS` in
  `scripts/purpleair_client.py` if you want them in the QC or the dashboard.

## Notes / limitations

- PurpleAir's history endpoint has fair-use rate limits and a point-count
  cap per request; a single day at 10-minute resolution (144 points/sensor)
  is well within normal limits for a city-sized network. If your network
  grows very large, consider fetching sensors in batches with a short sleep
  between them (the client already retries on HTTP 429).
- `last_seen`-based offline detection depends on your `offline_hours`
  threshold — too short and normal brief WiFi hiccups will flag as
  "offline"; too long and a genuinely dead sensor sits unnoticed longer.
- The A-vs-B check catches disagreement *between a sensor's own two
  channels* — it does not by itself confirm absolute accuracy against a
  reference monitor. For that you'd want an occasional co-location
  comparison against a regulatory-grade station, which is a separate
  (valuable) exercise from this daily automated QC.
