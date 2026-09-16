"""
Thin client for the PurpleAir REST API (https://api.purpleair.com).

Requires a PurpleAir API key with read access. Get one at:
https://develop.purpleair.com/  (sign in, "Keys" tab, create a READ key)

Set it as the environment variable PURPLEAIR_API_KEY.
"""
from __future__ import annotations

import os
import time
import logging
from datetime import datetime, timezone
from typing import Iterable

import requests

log = logging.getLogger("purpleair_client")

BASE_URL = "https://api.purpleair.com/v1"

SENSOR_LIST_FIELDS = [
    "name",
    "latitude",
    "longitude",
    "altitude",
    "last_seen",
    "last_modified",
    "date_created",
    "channel_state",   # 0=no PM, 1=PM_A, 2=PM_B, 3=PM_A+PM_B
    "channel_flags",   # 0=normal, 1=A downgraded, 2=B downgraded, 3=both downgraded
    "confidence",
    "pm2.5",
    "pm2.5_a",
    "pm2.5_b",
    "humidity",
    "temperature",
    "rssi",            # wifi signal strength
    "uptime",
    "location_type",   # 0=outside, 1=inside
]

HISTORY_FIELDS = [
    "pm1.0_cf_1_a",
    "pm1.0_cf_1_b",
    "pm2.5_cf_1_a",
    "pm2.5_cf_1_b",
    "pm10.0_cf_1_a",
    "pm10.0_cf_1_b",
    "humidity",
    "temperature",
    "rssi",
    "uptime",
    "memory",
]


class PurpleAirError(RuntimeError):
    pass


def _headers() -> dict:
    key = os.environ.get("PURPLEAIR_API_KEY")
    if not key:
        raise PurpleAirError(
            "PURPLEAIR_API_KEY environment variable is not set. "
            "Get a read key at https://develop.purpleair.com/"
        )
    return {"X-API-Key": key}


def _get(path: str, params: dict, retries: int = 3, backoff: float = 2.0) -> dict:
    url = f"{BASE_URL}/{path}"
    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, headers=_headers(), params=params, timeout=30)
            if resp.status_code == 429:
                wait = backoff * attempt
                log.warning("Rate limited (429). Waiting %.1fs before retry.", wait)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            last_exc = exc
            log.warning("Request failed (attempt %d/%d): %s", attempt, retries, exc)
            time.sleep(backoff * attempt)
    raise PurpleAirError(f"Failed to GET {url} after {retries} attempts: {last_exc}")


def get_sensors_in_bbox(bbox: dict, fields: Iterable[str] = SENSOR_LIST_FIELDS, max_age: int = 15552000) -> list[dict]:
    """
    Return current snapshot metadata for every sensor whose location falls
    inside the given bounding box. bbox = {"nwlat":..,"nwlng":..,"selat":..,"selng":..}
    """
    params = {
        "fields": ",".join(fields),
        "nwlat": bbox["nwlat"],
        "nwlng": bbox["nwlng"],
        "selat": bbox["selat"],
        "selng": bbox["selng"],
        "location_type": 0,  # outdoor sensors only; drop this line to include indoor
         "max_age": max_age,
    }
    payload = _get("sensors", params)
    field_names = payload["fields"]
    sensors = []
    for row in payload.get("data", []):
        sensors.append(dict(zip(field_names, row)))
    log.info("Found %d sensors in bounding box", len(sensors))
    return sensors


def get_sensor_history(
    sensor_index: int,
    start_ts: int,
    end_ts: int,
    average_minutes: int = 10,
    fields: Iterable[str] = HISTORY_FIELDS,
) -> list[dict]:
    """
    Return a list of {timestamp, <field>: value, ...} dicts for one sensor
    between start_ts and end_ts (unix seconds, UTC).
    """
    params = {
        "start_timestamp": start_ts,
        "end_timestamp": end_ts,
        "average": average_minutes,
        "fields": ",".join(fields),
    }
    payload = _get(f"sensors/{sensor_index}/history", params)
    field_names = payload["fields"]
    rows = []
    for row in payload.get("data", []):
        d = dict(zip(field_names, row))
        rows.append(d)
    return rows


def day_bounds_utc(date_str: str | None = None) -> tuple[int, int]:
    """
    Given 'YYYY-MM-DD' (or None for yesterday UTC), return (start_ts, end_ts)
    unix seconds spanning that whole UTC day.
    """
    if date_str is None:
        from datetime import timedelta
        d = datetime.now(timezone.utc) - timedelta(days=1)
        date_str = d.strftime("%Y-%m-%d")
    start = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end = start.replace(hour=23, minute=59, second=59)
    return int(start.timestamp()), int(end.timestamp())
