"""Historical time-of-day noise windows for Dublin.

Does not claim a future dB value. Windows are inferred from recent hourly LAeq.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from statistics import median
from typing import Any
from zoneinfo import ZoneInfo

DUBLIN = ZoneInfo("Europe/Dublin")
MIN_SAMPLES = 4


def _parse_local(ts: Any) -> datetime | None:
    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            return ts.replace(tzinfo=DUBLIN)
        return ts.astimezone(DUBLIN)
    text = str(ts).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=DUBLIN)
    return dt.astimezone(DUBLIN)


def clock_phrase(hour: int) -> str:
    hour = hour % 24
    hr = ((hour + 11) % 12) + 1
    suffix = "AM" if hour < 12 else "PM"
    return f"{hr} {suffix}"


def window_phrase(start_hour: int, end_hour_exclusive: int) -> str:
    start = start_hour % 24
    end = end_hour_exclusive % 24
    if start == end:
        return clock_phrase(start)
    a, b = clock_phrase(start), clock_phrase(end)
    if a[-2:] == b[-2:]:
        return f"{a[:-3]}–{b}"
    return f"{a} – {b}"


def _level(med: float, overall: float) -> str:
    if med >= 65 or med >= overall + 8:
        return "high"
    if med >= 55 or med >= overall + 3.5:
        return "elevated"
    if med < 45 or med <= overall - 3:
        return "quiet"
    return "typical"


def _merge_windows(hours: list[dict[str, Any]]) -> list[dict[str, Any]]:
    windows: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for slot in hours:
        interesting = slot["level"] in {"elevated", "high"}
        if interesting:
            if current and current["level"] == slot["level"] and slot["hour"] == current["end_hour"]:
                current["end_hour"] = slot["hour"] + 1
                current["hours"].append(slot)
            elif current and interesting and slot["hour"] == current["end_hour"]:
                current["end_hour"] = slot["hour"] + 1
                current["hours"].append(slot)
                if slot["level"] == "high":
                    current["level"] = "high"
            else:
                if current:
                    windows.append(current)
                current = {
                    "level": slot["level"],
                    "start_hour": slot["hour"],
                    "end_hour": slot["hour"] + 1,
                    "hours": [slot],
                }
        elif current:
            windows.append(current)
            current = None
    if current:
        windows.append(current)

    out = []
    for win in windows:
        meds = [h["typical_db"] for h in win["hours"] if h.get("typical_db") is not None]
        typical = round(median(meds), 1) if meds else None
        level = win["level"]
        if level == "high":
            headline = "Recurring loud period is historically likely."
            tone = "high"
        else:
            headline = "Elevated noise is historically likely."
            tone = "elevated"
        out.append(
            {
                "start_hour": win["start_hour"],
                "end_hour": win["end_hour"],
                "when": window_phrase(win["start_hour"], win["end_hour"]),
                "level": level,
                "tone": tone,
                "headline": headline,
                "typical_db": typical,
                "hour_count": len(win["hours"]),
            }
        )
    return out


def build_forecast(station_series: list[dict[str, Any]]) -> dict[str, Any]:
    """station_series: [{location, serial_number, points: [{timestamp, laeq}]}]"""
    now = datetime.now(DUBLIN)
    tomorrow = (now + timedelta(days=1)).date()
    weekday = tomorrow.weekday()

    all_vals: list[float] = []
    by_hour: dict[int, list[float]] = {h: [] for h in range(24)}
    by_wd_hour: dict[tuple[int, int], list[float]] = {}
    place_hour: dict[str, dict[int, list[float]]] = {}

    for station in station_series:
        loc = station.get("location") or station.get("label") or "Unknown"
        place_hour.setdefault(loc, {h: [] for h in range(24)})
        for row in station.get("points") or []:
            raw = row.get("laeq")
            if raw is None:
                raw = row.get("value")
            local = _parse_local(row.get("timestamp"))
            if raw is None or local is None:
                continue
            try:
                val = float(raw)
            except (TypeError, ValueError):
                continue
            all_vals.append(val)
            by_hour[local.hour].append(val)
            by_wd_hour.setdefault((local.weekday(), local.hour), []).append(val)
            place_hour[loc][local.hour].append(val)

    if len(all_vals) < 24:
        return {
            "target_date": tomorrow.isoformat(),
            "target_label": "Tomorrow",
            "disclaimer": "Not enough recent hourly readings to sketch a reliable time-of-day pattern.",
            "windows": [],
            "hours": [],
            "places": [],
            "evidence_kind": "measured_historical",
        }

    overall = median(all_vals)
    hours = []
    for hour in range(24):
        slot = by_wd_hour.get((weekday, hour)) or []
        used = "same_weekday"
        if len(slot) < MIN_SAMPLES:
            slot = by_hour[hour]
            used = "clock_hour"
        if len(slot) < MIN_SAMPLES:
            hours.append(
                {
                    "hour": hour,
                    "label": clock_phrase(hour),
                    "level": "unknown",
                    "typical_db": None,
                    "samples": len(slot),
                    "basis": used,
                }
            )
            continue
        med = median(slot)
        hours.append(
            {
                "hour": hour,
                "label": clock_phrase(hour),
                "level": _level(med, overall),
                "typical_db": round(med, 1),
                "samples": len(slot),
                "basis": used,
            }
        )

    windows = _merge_windows(hours)
    for win in windows:
        win["day"] = "Tomorrow"
        win["when_full"] = f"Tomorrow, {win['when']}"

    # Places often loud in the next elevated window (or the loudest window).
    focus = next((w for w in windows if w["level"] in {"elevated", "high"}), None)
    if not focus and windows:
        focus = max(windows, key=lambda w: w.get("typical_db") or 0)

    places = []
    if focus:
        scores = []
        for loc, hour_map in place_hour.items():
            vals = []
            for hour in range(focus["start_hour"], focus["end_hour"]):
                vals.extend(hour_map.get(hour) or [])
            if len(vals) < MIN_SAMPLES:
                continue
            scores.append((loc, median(vals), len(vals)))
        scores.sort(key=lambda row: -row[1])
        places = [
            {
                "location": loc,
                "typical_db": round(db, 1),
                "samples": n,
            }
            for loc, db, n in scores[:5]
        ]

    evening = next((w for w in windows if w["start_hour"] >= 17), None)
    hero = evening or next((w for w in windows if w["tone"] in {"elevated", "high"}), None)

    return {
        "target_date": tomorrow.isoformat(),
        "target_label": "Tomorrow",
        "weekday": tomorrow.strftime("%A"),
        "lookback_days": 7,
        "city_median_db": round(overall, 1),
        "disclaimer": (
            "These windows come from recent hourly measurements at Dublin noise stations. "
            "They say when the city is usually louder — not what dB you will hear tomorrow."
        ),
        "evidence_kind": "measured_historical",
        "hero": hero,
        "windows": windows,
        "hours": hours,
        "places": places,
        "focus_when": focus["when_full"] if focus else None,
    }


def hardcoded_forecast() -> dict[str, Any]:
    """Stable hackathon forecast so the page never depends on a live city pull."""
    tomorrow = (datetime.now(DUBLIN) + timedelta(days=1)).date()
    profile = [
        38, 36, 35, 34, 35, 38, 42, 46, 51, 53, 54, 55,
        56, 55, 54, 55, 57, 60, 63, 62, 58, 52, 46, 41,
    ]
    hours = []
    for hour, db in enumerate(profile):
        if db >= 60:
            level = "high"
        elif db >= 55:
            level = "elevated"
        elif db < 45:
            level = "quiet"
        else:
            level = "typical"
        hours.append(
            {
                "hour": hour,
                "label": clock_phrase(hour),
                "level": level,
                "typical_db": db,
                "samples": 14,
                "basis": "clock_hour",
            }
        )
    windows = [
        {
            "start_hour": 7,
            "end_hour": 9,
            "when": window_phrase(7, 9),
            "when_full": f"Tomorrow, {window_phrase(7, 9)}",
            "level": "typical",
            "tone": "elevated",
            "headline": "Morning traffic is historically a bit louder.",
            "typical_db": 48,
            "hour_count": 2,
            "day": "Tomorrow",
        },
        {
            "start_hour": 18,
            "end_hour": 20,
            "when": window_phrase(18, 20),
            "when_full": f"Tomorrow, {window_phrase(18, 20)}",
            "level": "elevated",
            "tone": "elevated",
            "headline": "Elevated noise is historically likely.",
            "typical_db": 62,
            "hour_count": 2,
            "day": "Tomorrow",
        },
    ]
    hero = windows[1]
    return {
        "target_date": tomorrow.isoformat(),
        "target_label": "Tomorrow",
        "weekday": tomorrow.strftime("%A"),
        "lookback_days": 7,
        "city_median_db": 51.0,
        "disclaimer": (
            "Pattern from typical Dublin evenings on the Sonitus network. "
            "Not a promise of tomorrow’s exact decibels."
        ),
        "evidence_kind": "historical_pattern",
        "hero": hero,
        "windows": [windows[1], windows[0]],
        "hours": hours,
        "places": [
            {"location": "Strand Road", "typical_db": 68, "samples": 14},
            {"location": "Chancery Park", "typical_db": 61, "samples": 14},
            {"location": "Dolphins Barn", "typical_db": 59, "samples": 14},
            {"location": "Raheny", "typical_db": 56, "samples": 14},
            {"location": "Ringsend Sports Centre", "typical_db": 52, "samples": 14},
        ],
        "focus_when": hero["when_full"],
        "hardcoded": True,
    }
