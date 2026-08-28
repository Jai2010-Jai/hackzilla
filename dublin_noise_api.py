#!/usr/bin/env python3
"""Dublin City Council / Smart Dublin Sonitus noise (and air) data client.

Verified against the official dataset:
  https://data.gov.ie/dataset/sonitus
  https://data.smartdublin.ie/dataset/sonitus
  Access URL: https://data.smartdublin.ie/sonitus-api

Do not invent extra endpoints or query parameters. Anything not listed in
VERIFIED_API below was not confirmed on the live API.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import pandas as pd
import requests
from dotenv import load_dotenv

VERIFIED_API = {
    "base_url": "https://data.smartdublin.ie/sonitus-api",
    "method": "POST",
    "auth": "JSON or form body fields username + password (HTTP Basic does not work)",
    "timezone_of_datetime_strings": "Europe/Dublin (naive local timestamps)",
    "timestamp_params": "Unix seconds in start and end",
    "rate_limit_headers": "X-RateLimit-Limit=100, X-RateLimit-Remaining (window not documented)",
    "pagination": "Not observed. One POST returns the full JSON array for the requested range.",
    "endpoints": {
        "monitors": {
            "path": "/api/monitors",
            "body": ["username", "password"],
            "returns": [
                "serial_number",
                "label",
                "location",
                "latitude",
                "longitude",
                "last_calibrated",
            ],
        },
        "monitor": {
            "path": "/api/monitor/{serial}",
            "body": ["username", "password"],
            "returns": [
                "serial_number",
                "label",
                "location",
                "latitude",
                "longitude",
                "last_calibrated",
            ],
        },
        "data": {
            "path": "/api/data",
            "body": ["username", "password", "monitor", "start", "end"],
            "notes": (
                "Highest-resolution historic readings. Noise monitors return "
                "approximately 5-minute LAeq statistics. Air monitors return "
                "pollutant fields such as pm2_5, pm10, no2, o3, so2."
            ),
        },
        "hourly-averages": {
            "path": "/api/hourly-averages",
            "body": ["username", "password", "monitor", "start", "end"],
            "returns_noise": ["datetime", "laeq"],
        },
        "noise-averages": {
            "path": "/api/noise-averages",
            "body": ["username", "password", "monitor", "start", "end"],
            "returns": [
                "date",
                "start_time",
                "end_time",
                "laeq",
                "limit_level",
                "breach",
            ],
            "notes": "Post-processed averages. Verified call returned one daily row.",
        },
    },
}

DUBLIN_TZ = ZoneInfo("Europe/Dublin")
UTC = ZoneInfo("UTC")
NOISE_LABEL_PREFIX = "Noise"

INTERVAL_TO_ENDPOINT = {
    "5min": "data",
    "raw": "data",
    "data": "data",
    "hourly": "hourly-averages",
    "hourly-averages": "hourly-averages",
    "daily": "noise-averages",
    "noise-averages": "noise-averages",
}


class SonitusAPIError(RuntimeError):
    """Raised when the official API returns an error payload or HTTP failure."""


def _load_settings() -> dict[str, str]:
    load_dotenv()
    username = os.getenv("SONITUS_USERNAME", "").strip()
    password = os.getenv("SONITUS_PASSWORD", "").strip()
    if not username or not password:
        raise SonitusAPIError(
            "Missing SONITUS_USERNAME / SONITUS_PASSWORD. "
            "Copy .env.example to .env (credentials are the public values "
            "published on data.gov.ie/dataset/sonitus)."
        )
    timeout = os.getenv("SONITUS_TIMEOUT_SECONDS", "60").strip() or "60"
    return {
        "username": username,
        "password": password,
        "base_url": os.getenv("SONITUS_BASE_URL", VERIFIED_API["base_url"]).rstrip("/"),
        "timeout": timeout,
    }


def local_date_to_unix_range(start_date: str, end_date: str) -> tuple[int, int]:
    """Convert inclusive calendar dates in Europe/Dublin to Unix start/end.

    `end_date` is inclusive: 2026-08-27 to 2026-08-27 covers that local day.
    The live API expects Unix seconds (confirmed by the official docs and by
    successful hourly-averages / data calls).
    """
    start_local = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=DUBLIN_TZ)
    end_day = datetime.strptime(end_date, "%Y-%m-%d").replace(tzinfo=DUBLIN_TZ)
    if end_day < start_local:
        raise ValueError("end_date must be on or after start_date")
    end_exclusive = end_day + timedelta(days=1)
    return int(start_local.timestamp()), int(end_exclusive.timestamp())


def _friendly_connection_error(path: str, exc: Exception) -> str:
    return (
        f"Dublin’s Sonitus API dropped the connection ({path}). "
        "This is usually a brief reset on data.smartdublin.ie, not an app bug. "
        f"Underlying error: {exc}"
    )


def _request_json(
    session: requests.Session,
    path: str,
    body: dict[str, Any],
    timeout: float,
    retries: int = 5,
) -> Any:
    last_error: Exception | None = None
    headers = {
        "Accept": "application/json",
        "User-Agent": "dublin-noise-api/1.0",
        # Keep-alive sockets to smartdublin are often reset mid-request.
        "Connection": "close",
    }
    for attempt in range(1, retries + 1):
        try:
            response = session.post(
                path,
                json=body,
                timeout=timeout,
                headers=headers,
            )
        except (requests.Timeout, requests.ConnectionError, requests.ChunkedEncodingError) as exc:
            last_error = exc
            time.sleep(min(1.5 ** attempt, 10))
            continue
        except requests.RequestException as exc:
            last_error = exc
            time.sleep(min(1.5 ** attempt, 10))
            continue

        remaining = response.headers.get("X-RateLimit-Remaining")
        if remaining is not None:
            try:
                if int(remaining) <= 5:
                    time.sleep(1.0)
            except ValueError:
                pass

        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After")
            wait_s = float(retry_after) if retry_after and retry_after.isdigit() else 10.0
            time.sleep(wait_s)
            last_error = SonitusAPIError("HTTP 429 rate limited")
            continue

        if response.status_code >= 400:
            raise SonitusAPIError(
                f"HTTP {response.status_code} for {path}: {response.text[:500]}"
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise SonitusAPIError(
                f"Non-JSON response from {path}: {response.text[:300]}"
            ) from exc

        if isinstance(payload, dict) and "error" in payload:
            raise SonitusAPIError(f"API error for {path}: {payload['error']}")
        if isinstance(payload, dict) and "message" in payload and len(payload) == 1:
            raise SonitusAPIError(f"API message for {path}: {payload['message']}")
        return payload

    raise SonitusAPIError(_friendly_connection_error(path, last_error))


class SonitusClient:
    def __init__(self) -> None:
        settings = _load_settings()
        self.username = settings["username"]
        self.password = settings["password"]
        self.base_url = settings["base_url"]
        self.timeout = float(settings["timeout"])
        self.session = requests.Session()
        self.session.headers.update({"Connection": "close"})
        self._cache_path = Path(__file__).resolve().parent / "data" / "monitors_cache.json"
        self.monitors_cached = False

    def _auth_body(self, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {
            "username": self.username,
            "password": self.password,
        }
        if extra:
            body.update(extra)
        return body

    def list_monitors(self) -> list[dict[str, Any]]:
        cached = self._read_monitors_cache()
        if cached is not None:
            self.monitors_cached = True
            return cached
        self.monitors_cached = False
        try:
            payload = _request_json(
                self.session,
                f"{self.base_url}/api/monitors",
                self._auth_body(),
                self.timeout,
            )
        except SonitusAPIError:
            raise
        if not isinstance(payload, list):
            raise SonitusAPIError("Unexpected /api/monitors response: expected a JSON array")
        self._write_monitors_cache(payload)
        return payload

    def _read_monitors_cache(self) -> list[dict[str, Any]] | None:
        if not self._cache_path.exists():
            return None
        try:
            payload = json.loads(self._cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if isinstance(payload, list) and payload:
            return payload
        return None

    def _write_monitors_cache(self, payload: list[dict[str, Any]]) -> None:
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            self._cache_path.write_text(json.dumps(payload), encoding="utf-8")
        except OSError:
            return

    def get_monitor(self, serial: str) -> dict[str, Any]:
        payload = _request_json(
            self.session,
            f"{self.base_url}/api/monitor/{serial}",
            self._auth_body(),
            self.timeout,
        )
        if not isinstance(payload, dict):
            raise SonitusAPIError("Unexpected /api/monitor/{serial} response")
        return payload

    def fetch_readings(
        self,
        endpoint: str,
        monitor: str,
        start_unix: int,
        end_unix: int,
    ) -> list[dict[str, Any]]:
        if endpoint not in {"data", "hourly-averages", "noise-averages"}:
            raise ValueError(
                "endpoint must be one of: data, hourly-averages, noise-averages "
                "(the only reading endpoints documented and verified)"
            )
        payload = _request_json(
            self.session,
            f"{self.base_url}/api/{endpoint}",
            self._auth_body(
                {
                    "monitor": monitor,
                    "start": start_unix,
                    "end": end_unix,
                }
            ),
            self.timeout,
        )
        if not isinstance(payload, list):
            raise SonitusAPIError(f"Unexpected /api/{endpoint} response: expected a JSON array")
        return payload


def is_noise_monitor(monitor: dict[str, Any]) -> bool:
    label = str(monitor.get("label") or "")
    return label.startswith(NOISE_LABEL_PREFIX)


def classify_monitor(monitor: dict[str, Any]) -> str:
    label = str(monitor.get("label") or "")
    if label.startswith(NOISE_LABEL_PREFIX):
        return "noise"
    if "Air" in label or label.startswith("Gas"):
        return "air"
    return "other"


def _normalize_timestamps(df: pd.DataFrame) -> pd.DataFrame:
    if "datetime" in df.columns:
        parsed = pd.to_datetime(df["datetime"], errors="coerce")
        # API datetimes are naive local Europe/Dublin values.
        df["timestamp"] = parsed.dt.tz_localize(
            DUBLIN_TZ, ambiguous="NaT", nonexistent="shift_forward"
        )
        df["timestamp_utc"] = df["timestamp"].dt.tz_convert(UTC)
    elif "date" in df.columns:
        date_part = pd.to_datetime(df["date"], errors="coerce")
        if "start_time" in df.columns:
            time_part = pd.to_timedelta(df["start_time"].astype(str))
            combined = date_part + time_part
        else:
            combined = date_part
        df["timestamp"] = combined.dt.tz_localize(
            DUBLIN_TZ, ambiguous="NaT", nonexistent="shift_forward"
        )
        df["timestamp_utc"] = df["timestamp"].dt.tz_convert(UTC)
    return df


def readings_to_frame(
    rows: list[dict[str, Any]],
    monitor_meta: dict[str, Any],
    endpoint: str,
) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["serial_number"] = monitor_meta.get("serial_number")
    df["label"] = monitor_meta.get("label")
    df["location"] = monitor_meta.get("location")
    df["latitude"] = pd.to_numeric(monitor_meta.get("latitude"), errors="coerce")
    df["longitude"] = pd.to_numeric(monitor_meta.get("longitude"), errors="coerce")
    df["last_calibrated"] = monitor_meta.get("last_calibrated")
    df["source_endpoint"] = endpoint
    df = _normalize_timestamps(df)
    preferred = [
        "timestamp",
        "timestamp_utc",
        "serial_number",
        "label",
        "location",
        "latitude",
        "longitude",
        "datetime",
        "date",
        "start_time",
        "end_time",
        "laeq",
        "lafmax",
        "la10",
        "la90",
        "lceq",
        "lcfmax",
        "lc10",
        "lc90",
        "limit_level",
        "breach",
        "source_endpoint",
        "last_calibrated",
    ]
    ordered = [c for c in preferred if c in df.columns] + [
        c for c in df.columns if c not in preferred
    ]
    return df[ordered]


def get_noise_data(
    start_date: str,
    end_date: str,
    location: str = "all",
    interval: str = "5min",
    fields: Iterable[str] | None = None,
    max_records: int | None = None,
    include_air: bool = False,
    save_raw_json: Path | None = None,
) -> pd.DataFrame:
    """Fetch readings for one monitor serial, or all noise monitors.

    Parameters match verified API capabilities:
      start_date / end_date: inclusive calendar dates (YYYY-MM-DD), converted
        to Unix `start` / `end` in Europe/Dublin.
      location: monitor serial_number, or "all"
      interval: 5min/raw/data | hourly | daily  (maps to verified endpoints)
      fields: optional client-side column subset (API has no fields parameter)
      max_records: optional client-side cap (API has no limit/pagination params)
    """
    endpoint = INTERVAL_TO_ENDPOINT.get(interval)
    if endpoint is None:
        raise ValueError(
            f"Unsupported interval {interval!r}. Verified options: "
            + ", ".join(sorted(INTERVAL_TO_ENDPOINT))
        )

    client = SonitusClient()
    start_unix, end_unix = local_date_to_unix_range(start_date, end_date)
    monitors = client.list_monitors()
    by_serial = {str(m.get("serial_number")): m for m in monitors}

    if location in {"all", "*", "ALL"}:
        selected = monitors if include_air else [m for m in monitors if is_noise_monitor(m)]
    else:
        if location not in by_serial:
            known = ", ".join(sorted(by_serial))
            raise SonitusAPIError(
                f"Unknown monitor serial {location!r}. Known serial_number values: {known}"
            )
        selected = [by_serial[location]]

    raw_dump: dict[str, Any] = {
        "endpoint": endpoint,
        "start_unix": start_unix,
        "end_unix": end_unix,
        "monitors": [],
    }
    frames: list[pd.DataFrame] = []
    remaining = max_records

    for meta in selected:
        serial = str(meta["serial_number"])
        rows = client.fetch_readings(endpoint, serial, start_unix, end_unix)
        raw_dump["monitors"].append({"serial_number": serial, "rows": rows})
        frame = readings_to_frame(rows, meta, endpoint)
        if remaining is not None:
            if remaining <= 0:
                break
            if len(frame) > remaining:
                frame = frame.iloc[:remaining].copy()
            remaining -= len(frame)
        frames.append(frame)

    if save_raw_json is not None:
        save_raw_json.parent.mkdir(parents=True, exist_ok=True)
        save_raw_json.write_text(json.dumps(raw_dump, indent=2), encoding="utf-8")

    if not frames:
        return pd.DataFrame()

    df = pd.concat(frames, ignore_index=True)
    if fields:
        keep = [c for c in fields if c in df.columns]
        missing = [c for c in fields if c not in df.columns]
        if missing:
            print(f"Note: requested fields not in response and skipped: {missing}", file=sys.stderr)
        df = df[keep]
    return df


def get_all_dublin_sensors(
    start_date: str,
    end_date: str,
    interval: str = "5min",
    include_air: bool = False,
    max_records: int | None = None,
    save_raw_json: Path | None = None,
) -> pd.DataFrame:
    """Retrieve the requested interval for every available Dublin monitor.

    Default is noise monitors only (label starts with 'Noise'). Pass
    include_air=True to also pull air-quality stations from the same API.
    """
    return get_noise_data(
        start_date=start_date,
        end_date=end_date,
        location="all",
        interval=interval,
        max_records=max_records,
        include_air=include_air,
        save_raw_json=save_raw_json,
    )


def print_summary(df: pd.DataFrame) -> None:
    print("=== Dublin Sonitus download summary ===")
    print(f"records: {len(df)}")
    if df.empty:
        print("No rows returned.")
        return
    if "timestamp" in df.columns and df["timestamp"].notna().any():
        print(f"date range: {df['timestamp'].min()} -> {df['timestamp'].max()}")
    elif "datetime" in df.columns:
        print(f"datetime range: {df['datetime'].min()} -> {df['datetime'].max()}")
    sensors = df["serial_number"].nunique() if "serial_number" in df.columns else "n/a"
    print(f"sensors: {sensors}")
    print(f"columns: {', '.join(df.columns)}")
    if "laeq" in df.columns:
        laeq = pd.to_numeric(df["laeq"], errors="coerce")
        print(f"min LAeq: {laeq.min()}")
        print(f"max LAeq: {laeq.max()}")
        print(f"mean LAeq: {laeq.mean()}")
    else:
        print("No laeq column in this extract (air-only or filtered columns).")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Download Dublin City Council Sonitus noise/air readings."
    )
    parser.add_argument("--start", help="Inclusive start date YYYY-MM-DD")
    parser.add_argument("--end", help="Inclusive end date YYYY-MM-DD")
    parser.add_argument(
        "--monitor",
        default="all",
        help="Monitor serial_number, or 'all' (default: all noise monitors)",
    )
    parser.add_argument(
        "--interval",
        default="5min",
        help="5min (api/data), hourly (api/hourly-averages), daily (api/noise-averages)",
    )
    parser.add_argument(
        "--fields",
        default="",
        help="Optional comma-separated column subset applied after download",
    )
    parser.add_argument("--max-records", type=int, default=None)
    parser.add_argument(
        "--all-sensors",
        action="store_true",
        help="Fetch every noise monitor (same as --monitor all)",
    )
    parser.add_argument(
        "--include-air",
        action="store_true",
        help="When selecting all monitors, also include air-quality stations",
    )
    parser.add_argument("--list-monitors", action="store_true")
    parser.add_argument("--noise-only-list", action="store_true")
    parser.add_argument(
        "--output",
        default="",
        help="CSV output path (default: data/dublin_noise_<start>_<end>.csv)",
    )
    parser.add_argument(
        "--save-json",
        default="",
        help="Optional path to save the raw API JSON payload",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.list_monitors or args.noise_only_list:
        client = SonitusClient()
        monitors = client.list_monitors()
        if args.noise_only_list:
            monitors = [m for m in monitors if is_noise_monitor(m)]
        print(json.dumps(monitors, indent=2))
        print(f"# {len(monitors)} monitors", file=sys.stderr)
        return 0

    if not args.start or not args.end:
        parser.error("--start and --end are required unless --list-monitors is used")

    location = "all" if args.all_sensors else args.monitor
    field_list = [f.strip() for f in args.fields.split(",") if f.strip()] or None
    raw_path = Path(args.save_json) if args.save_json else None

    df = get_noise_data(
        start_date=args.start,
        end_date=args.end,
        location=location,
        interval=args.interval,
        fields=field_list,
        max_records=args.max_records,
        include_air=args.include_air,
        save_raw_json=raw_path,
    )

    out = Path(args.output) if args.output else Path("data") / f"dublin_noise_{args.start}_{args.end}.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out, index=False, quoting=csv.QUOTE_NONNUMERIC)
    print_summary(df)
    print(f"saved CSV: {out}")
    if raw_path:
        print(f"saved raw JSON: {raw_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
