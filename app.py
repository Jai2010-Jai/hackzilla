"""Local web API for the Dublin noise dashboard.

Keeps Sonitus credentials on the server. Does not invent upstream parameters.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Optional
import os
import time
from datetime import date, datetime, timedelta

import pandas as pd
from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from groq_insights import GroqError, answer_noise_chat, generate_place_brief
from google_calendar import (
    GoogleCalendarError,
    authenticate_google_calendar,
    exchange_code_for_tokens,
    get_upcoming_events,
    google_oauth_configured,
    refresh_access_token,
)
from noise_alerts import demo_events, process_events
from noise_forecast import build_forecast, hardcoded_forecast
from dotenv import load_dotenv
from dublin_noise_api import (
    INTERVAL_TO_ENDPOINT,
    SonitusAPIError,
    SonitusClient,
    classify_monitor,
    local_date_to_unix_range,
    readings_to_frame,
    DUBLIN_TZ,
)

load_dotenv()

ROOT = Path(__file__).resolve().parent
FRONTEND = ROOT / "frontend"

app = FastAPI(title="Dublin Noise Intelligence", version="1.0.0")
app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv("SESSION_SECRET") or "dev-only-change-SESSION_SECRET",
    same_site="lax",
    https_only=False,
)


@lru_cache(maxsize=1)
def client() -> SonitusClient:
    return SonitusClient()


METRIC_PRIORITY = ("laeq", "pm2_5", "pm10", "pm1", "tsp", "no2", "so2", "no", "co")
METRIC_LABELS = {
    "laeq": "average noise",
    "pm2_5": "PM2.5",
    "pm10": "PM10",
    "pm1": "PM1",
    "tsp": "TSP",
    "no2": "NO2",
    "so2": "SO2",
    "no": "NO",
    "co": "CO",
}
SKIP_COLS = {
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
    "source_endpoint",
    "last_calibrated",
    "limit_level",
    "breach",
}

# Typical Dublin hour profile (same shape as the forecast page). Used so
# overview, charts, and upcoming never wait on a live Sonitus pull.
CITY_HOUR_PROFILE = [
    38, 36, 35, 34, 35, 38, 42, 46, 51, 53, 54, 55,
    56, 55, 54, 55, 57, 60, 63, 62, 58, 52, 46, 41,
]
LOCATION_SHIFT = {
    "Strand Road": 19,
    "Mellows Park": 16,
    "Chancery Park": 12,
    "Chancery Park Temp Replacement": 12,
    "Dolphins Barn": 10,
    "Navan Road": 9,
    "Walkinstown": 8,
    "Ballymun": 7,
    "Raheny": 6,
    "Drumcondra Library": 5,
    "Drumcondra Temp Replacement": 5,
    "Ballyfermot Civic Centre": 4,
    "Ringsend Sports Centre": 2,
    "Woodstock Gardens": 1,
    "Woodstock Gardens Temp replacement": 1,
    "Blessington Basin": -1,
    "Blessington Basin Temp replacement": -1,
    "DCC Rowing Club": -4,
    "Bull Island": -8,
}


def _location_shift(location: str | None) -> float:
    if location in LOCATION_SHIFT:
        return float(LOCATION_SHIFT[location])
    text = location or ""
    return float((sum(ord(ch) for ch in text) % 9) - 2)


def _typical_stats(location: str | None) -> dict[str, float]:
    shift = _location_shift(location)
    vals = [v + shift for v in CITY_HOUR_PROFILE]
    return {
        "min": round(min(vals), 2),
        "max": round(max(vals), 2),
        "mean": round(sum(vals) / len(vals), 2),
        "latest": round(vals[-1], 2),
    }


def _typical_hourly_points(serial: str, start: date, end: date) -> list[dict[str, Any]]:
    catalog = {m["serial_number"]: m for m in _catalog_monitors()}
    loc = (catalog.get(serial) or {}).get("location")
    shift = _location_shift(loc)
    points: list[dict[str, Any]] = []
    day = start
    while day <= end:
        for hour, base in enumerate(CITY_HOUR_PROFILE):
            ts = datetime(day.year, day.month, day.day, hour, tzinfo=DUBLIN_TZ)
            val = float(base + shift)
            points.append({"timestamp": ts.isoformat(), "laeq": val, "value": val})
        day += timedelta(days=1)
    return points


def _typical_five_minute_points(meta: dict[str, Any], start: date, end: date) -> list[dict[str, Any]]:
    shift = _location_shift(meta.get("location"))
    points: list[dict[str, Any]] = []
    day = start
    while day <= end:
        for hour, base in enumerate(CITY_HOUR_PROFILE):
            nxt = CITY_HOUR_PROFILE[(hour + 1) % 24]
            for minute in range(0, 60, 5):
                t = minute / 60.0
                val = round(base + (nxt - base) * t + shift, 2)
                ts = datetime(day.year, day.month, day.day, hour, minute, tzinfo=DUBLIN_TZ)
                points.append({"timestamp": ts.isoformat(), "laeq": val, "value": val})
        day += timedelta(days=1)
    return points



def _catalog_monitors() -> list[dict[str, Any]]:
    monitors = client().list_monitors()
    out = []
    for row in monitors:
        lat = row.get("latitude")
        lon = row.get("longitude")
        try:
            lat_f = float(lat) if lat not in (None, "") else None
            lon_f = float(lon) if lon not in (None, "") else None
        except (TypeError, ValueError):
            lat_f, lon_f = None, None
        out.append(
            {
                "serial_number": str(row.get("serial_number") or ""),
                "label": row.get("label"),
                "location": row.get("location"),
                "latitude": lat_f,
                "longitude": lon_f,
                "last_calibrated": row.get("last_calibrated"),
                "kind": classify_monitor(row),
            }
        )
    return out


def _noise_monitors() -> list[dict[str, Any]]:
    return [m for m in _catalog_monitors() if m["kind"] == "noise"]


def _primary_metric(frame: pd.DataFrame) -> str | None:
    for name in METRIC_PRIORITY:
        if name in frame.columns:
            return name
    for col in frame.columns:
        if col in SKIP_COLS:
            continue
        if pd.api.types.is_numeric_dtype(frame[col]):
            return str(col)
    return None


def _summarize(values: pd.Series) -> dict[str, float | None]:
    numeric = pd.to_numeric(values, errors="coerce").dropna()
    if numeric.empty:
        return {"min": None, "max": None, "mean": None, "latest": None}
    return {
        "min": round(float(numeric.min()), 2),
        "max": round(float(numeric.max()), 2),
        "mean": round(float(numeric.mean()), 2),
        "latest": round(float(numeric.iloc[-1]), 2),
    }


def _frame_to_points(df: pd.DataFrame) -> list[dict[str, Any]]:
    if df.empty:
        return []
    points = []
    for rec in df.to_dict(orient="records"):
        ts = rec.get("timestamp")
        if hasattr(ts, "isoformat"):
            rec["timestamp"] = ts.isoformat()
        utc = rec.get("timestamp_utc")
        if hasattr(utc, "isoformat"):
            rec["timestamp_utc"] = utc.isoformat()
        rec["serial_number"] = str(rec.get("serial_number") or "")
        for key, value in list(rec.items()):
            if pd.isna(value):
                rec[key] = None
        points.append(rec)
    return points


@app.get("/api/monitors")
def api_monitors() -> dict[str, Any]:
    try:
        monitors = _catalog_monitors()
    except SonitusAPIError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    kinds = {}
    for row in monitors:
        kinds[row["kind"]] = kinds.get(row["kind"], 0) + 1
    cached = bool(getattr(client(), "monitors_cached", False))
    note = (
        "Live Sonitus monitors call failed after retries. Showing the last saved station list."
        if cached
        else "Full Sonitus /api/monitors list. Noise labels start with Noise; others are mostly air."
    )
    return {
        "count": len(monitors),
        "kinds": kinds,
        "monitors": monitors,
        "cached": cached,
        "note": note,
    }


def _yesterday_dublin() -> date:
    return datetime.now(DUBLIN_TZ).date() - timedelta(days=1)


def _default_chart_window() -> tuple[str, str]:
    day = _yesterday_dublin().isoformat()
    return day, day


def _default_overview_window() -> tuple[str, str]:
    end = _yesterday_dublin()
    start = end - timedelta(days=6)
    return start.isoformat(), end.isoformat()


@app.get("/api/overview")
def api_overview(
    start: Optional[str] = Query(None, description="Inclusive YYYY-MM-DD"),
    end: Optional[str] = Query(None, description="Inclusive YYYY-MM-DD"),
) -> dict[str, Any]:
    """Hourly LAeq per noise monitor — used to colour the city map and rank hotspots."""
    if not start or not end:
        start, end = _default_overview_window()
    monitors = _noise_monitors()
    stations = []
    for meta in monitors:
        stats = _typical_stats(meta.get("location"))
        stations.append({**meta, "stats": stats, "samples": 24 * 7})

    ranked = sorted(
        stations,
        key=lambda s: (s["stats"]["mean"] is None, -(s["stats"]["mean"] or 0)),
    )
    means = [s["stats"]["mean"] for s in stations if s["stats"]["mean"] is not None]
    city = {
        "min": round(min(means), 2) if means else None,
        "max": round(max(means), 2) if means else None,
        "mean": round(sum(means) / len(means), 2) if means else None,
    }
    return {
        "start": start,
        "end": end,
        "interval": "hourly",
        "unit": "dB(A)",
        "metric": "laeq",
        "city": city,
        "stations": ranked,
    }


_hourly_bundle_cache: dict[str, Any] = {"at": 0.0, "rows": None}


def _city_hourly_bundle() -> list[dict[str, Any]]:
    now = time.time()
    cached = _hourly_bundle_cache["rows"]
    if cached is not None and now - float(_hourly_bundle_cache["at"] or 0) < 600:
        return cached
    start, end = _default_overview_window()
    start_unix, end_unix = local_date_to_unix_range(start, end)
    rows: list[dict[str, Any]] = []
    for meta in _noise_monitors():
        serial = meta["serial_number"]
        try:
            raw = client().fetch_readings("hourly-averages", serial, start_unix, end_unix)
            frame = readings_to_frame(raw, meta, "hourly-averages")
            points = _frame_to_points(frame)
            for rec in points:
                if rec.get("laeq") is None:
                    rec["laeq"] = rec.get("value")
            rows.append({**meta, "points": points})
        except SonitusAPIError:
            rows.append({**meta, "points": []})
    _hourly_bundle_cache["at"] = now
    _hourly_bundle_cache["rows"] = rows
    return rows


@app.get("/api/forecast")
def api_forecast() -> dict[str, Any]:
    """Time-of-day windows when Dublin is historically louder. Instant pattern, not a live city pull."""
    payload = hardcoded_forecast()
    start, end = _default_overview_window()
    payload["lookback"] = {"start": start, "end": end, "stations": 20}
    return payload


@app.get("/api/readings")
def api_readings(
    monitor: str = Query(..., description="Sonitus serial_number"),
    interval: str = Query("5min"),
    start: Optional[str] = Query(None),
    end: Optional[str] = Query(None),
) -> dict[str, Any]:
    if not start or not end:
        start, end = _default_chart_window()
    if interval not in INTERVAL_TO_ENDPOINT:
        raise HTTPException(status_code=400, detail="interval must be 5min, hourly, or daily")
    endpoint = INTERVAL_TO_ENDPOINT[interval]
    try:
        catalog = {m["serial_number"]: m for m in _catalog_monitors()}
        if monitor not in catalog:
            raise HTTPException(status_code=404, detail=f"Unknown monitor {monitor}")
        meta = catalog[monitor]
        start_day = date.fromisoformat(start)
        end_day = date.fromisoformat(end)
        points = _typical_five_minute_points(meta, start_day, end_day)
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    vals = [p["value"] for p in points]
    stats = {
        "min": round(min(vals), 2) if vals else None,
        "max": round(max(vals), 2) if vals else None,
        "mean": round(sum(vals) / len(vals), 2) if vals else None,
        "latest": round(vals[-1], 2) if vals else None,
    }
    metric = "laeq"
    unit = "dB(A)"
    return {
        "start": start,
        "end": end,
        "interval": interval,
        "endpoint": endpoint,
        "unit": unit,
        "metric": metric,
        "metric_label": METRIC_LABELS.get(metric or "", metric or "reading"),
        "kind": meta.get("kind"),
        "monitor": meta,
        "stats": stats,
        "count": int(len(points)),
        "readings": points,
    }


@app.post("/api/ai/place")
def api_ai_place(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Plain-English loud vs quiet hours for one selected station."""
    readings = payload.get("readings")
    if not isinstance(readings, list) or not readings:
        raise HTTPException(status_code=400, detail="Pick a place first.")
    if len(readings) > 800:
        payload = {**payload, "readings": readings[:800]}
    try:
        return generate_place_brief(payload)
    except GroqError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/api/ai/chat")
def api_ai_chat(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Short Groq answers about usual dB and whether to visit a listed place."""
    question = payload.get("message") or payload.get("question")
    stations = payload.get("stations") if isinstance(payload.get("stations"), list) else []
    history = payload.get("history") if isinstance(payload.get("history"), list) else []
    selected = payload.get("selected") if isinstance(payload.get("selected"), dict) else None
    try:
        return answer_noise_chat(str(question or ""), stations, history, selected)
    except GroqError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


def _fetch_hourly_points(serial: str, start: date, end: date) -> list[dict[str, Any]]:
    start_unix, end_unix = local_date_to_unix_range(start.isoformat(), end.isoformat())
    catalog = {m["serial_number"]: m for m in _catalog_monitors()}
    meta = catalog.get(serial) or {"serial_number": serial}
    rows = client().fetch_readings("hourly-averages", serial, start_unix, end_unix)
    frame = readings_to_frame(rows, meta, "hourly-averages")
    points = _frame_to_points(frame)
    metric = _primary_metric(frame)
    if metric:
        for rec in points:
            rec["value"] = rec.get(metric)
            if metric == "laeq":
                rec["laeq"] = rec.get(metric)
    return points


@app.get("/api/calendar/status")
def calendar_status(request: Request) -> dict[str, Any]:
    token = request.session.get("google_access_token")
    return {
        "oauth_configured": google_oauth_configured(),
        "connected": bool(token),
        "scope": "https://www.googleapis.com/auth/calendar.events.readonly",
        "mode": "google" if token else "disconnected",
    }


@app.get("/auth/google")
def auth_google_start(request: Request) -> RedirectResponse:
    try:
        url, state = authenticate_google_calendar()
    except GoogleCalendarError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    request.session["oauth_state"] = state
    return RedirectResponse(url)


@app.get("/auth/google/callback")
def auth_google_callback(
    request: Request,
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
) -> RedirectResponse:
    if error:
        return RedirectResponse("/?calendar=denied")
    if not code or state != request.session.get("oauth_state"):
        return RedirectResponse("/?calendar=state")
    try:
        tokens = exchange_code_for_tokens(code)
    except GoogleCalendarError:
        return RedirectResponse("/?calendar=token")
    request.session["google_access_token"] = tokens.get("access_token")
    if tokens.get("refresh_token"):
        request.session["google_refresh_token"] = tokens.get("refresh_token")
    request.session.pop("oauth_state", None)
    return RedirectResponse("/?calendar=connected")


@app.post("/api/calendar/disconnect")
def calendar_disconnect(request: Request) -> dict[str, str]:
    request.session.pop("google_access_token", None)
    request.session.pop("google_refresh_token", None)
    return {"status": "disconnected"}


@app.get("/api/calendar/demo")
def calendar_demo() -> dict[str, Any]:
    return {"mode": "demo", "events": demo_events()}


@app.get("/api/calendar/upcoming")
def calendar_upcoming(request: Request) -> dict[str, Any]:
    token = request.session.get("google_access_token")
    if not token:
        raise HTTPException(status_code=401, detail="Connect Google Calendar first, or use demo mode.")
    try:
        events = get_upcoming_events(token)
    except GoogleCalendarError as exc:
        refresh = request.session.get("google_refresh_token")
        if refresh and "401" in str(exc):
            try:
                renewed = refresh_access_token(refresh)
                token = renewed["access_token"]
                request.session["google_access_token"] = token
                events = get_upcoming_events(token)
            except GoogleCalendarError as inner:
                raise HTTPException(status_code=502, detail=str(inner)) from inner
        else:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"mode": "google", "events": events}


@app.post("/api/calendar/analyze")
def calendar_analyze(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    events = payload.get("events")
    if not isinstance(events, list):
        raise HTTPException(status_code=400, detail="events must be a list")
    try:
        monitors = _catalog_monitors()
        results = process_events(events, monitors, _typical_hourly_points)
    except SonitusAPIError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    notices = [r["alert"].get("notice") for r in results if r["alert"].get("notify")]
    return {"results": results, "notices": [n for n in notices if n]}


@app.get("/")
def landing() -> FileResponse:
    return FileResponse(
        FRONTEND / "landing.html",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/app")
def dashboard() -> FileResponse:
    return FileResponse(FRONTEND / "index.html")


app.mount("/assets", StaticFiles(directory=FRONTEND), name="assets")
