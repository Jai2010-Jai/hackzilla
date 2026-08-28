"""Match calendar events to Sonitus noise monitors and score historical patterns.

Does not predict a future dB value. Alerts are inferences from past hourly LAeq
at the matched station, for the same clock hour on recent days.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from statistics import median
from typing import Any, Callable
from zoneinfo import ZoneInfo

DUBLIN = ZoneInfo("Europe/Dublin")
MIN_HOUR_SAMPLES = 4
LOOKBACK_DAYS = 14


def normalize_location(text: str) -> str:
    lowered = text.lower()
    lowered = re.sub(r"[^\w\s]", " ", lowered)
    lowered = re.sub(r"\s+", " ", lowered).strip()
    return lowered


def _tokens(text: str) -> set[str]:
    return {part for part in normalize_location(text).split() if len(part) >= 3}


def match_location_to_noise_data(
    location: str | None,
    monitors: list[dict[str, Any]],
) -> dict[str, Any]:
    if not location or not str(location).strip():
        return {"matched": False, "reason": "no_location", "monitor": None, "score": 0}
    query = normalize_location(location)
    q_tokens = _tokens(location)
    if not query:
        return {"matched": False, "reason": "no_location", "monitor": None, "score": 0}

    noise = [m for m in monitors if m.get("kind") == "noise"]
    best = None
    best_score = 0.0
    for mon in noise:
        hay = normalize_location(f"{mon.get('location') or ''} {mon.get('label') or ''}")
        if not hay:
            continue
        ratio = SequenceMatcher(None, query, hay).ratio()
        overlap = len(q_tokens & _tokens(hay))
        score = ratio + 0.15 * overlap
        if query in hay or hay in query:
            score += 0.35
        if score > best_score:
            best_score = score
            best = mon

    if best is None:
        return {
            "matched": False,
            "reason": "unable_to_match",
            "monitor": None,
            "score": 0,
            "hint": "Calendar location did not clearly match a Dublin Sonitus noise station. No geocoding was used.",
        }
    station_tokens = _tokens(str(best.get("location") or "")) | _tokens(str(best.get("label") or ""))
    shared = q_tokens & station_tokens
    distinctive = {tok for tok in shared if len(tok) >= 5}
    # Require a real token overlap so "Phoenix Marketcity" cannot silently become Raheny.
    if best_score < 0.62 or (not distinctive and len(shared) < 2):
        return {
            "matched": False,
            "reason": "unable_to_match",
            "monitor": None,
            "score": round(best_score, 3),
            "hint": "Calendar location did not clearly match a Dublin Sonitus noise station. No geocoding was used.",
        }
    return {
        "matched": True,
        "reason": "string_match",
        "monitor": best,
        "score": round(best_score, 3),
    }


def _parse_event_local(start_raw: str) -> datetime | None:
    text = str(start_raw).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        try:
            dt = datetime.fromisoformat(text + "T09:00:00")
        except ValueError:
            return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=DUBLIN)
    return dt.astimezone(DUBLIN)


def _hour_key(ts: datetime) -> tuple[int, int]:
    return ts.weekday(), ts.hour


def analyze_noise_for_event(
    event: dict[str, Any],
    hourly_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """Compare the event's local hour to recent hourly LAeq at one station."""
    start_local = _parse_event_local(event.get("start") or "")
    if start_local is None:
        return {
            "level": "unknown",
            "title": "Insufficient historical data to generate a reliable noise warning.",
            "evidence_kind": "none",
        }

    values: list[tuple[datetime, float]] = []
    for row in hourly_rows:
        raw = row.get("laeq")
        if raw is None:
            raw = row.get("value")
        ts = row.get("timestamp")
        if raw is None or not ts:
            continue
        try:
            val = float(raw)
        except (TypeError, ValueError):
            continue
        if isinstance(ts, datetime):
            local = ts.astimezone(DUBLIN) if ts.tzinfo else ts.replace(tzinfo=DUBLIN)
        else:
            local = _parse_event_local(str(ts))
        if local is None:
            continue
        values.append((local, val))

    if len(values) < 12:
        return {
            "level": "insufficient",
            "label": "Insufficient data",
            "title": "Insufficient historical data to generate a reliable noise warning.",
            "evidence_kind": "measured_historical",
            "sample_count": len(values),
            "event_local": start_local.isoformat(),
        }

    all_laeq = [v for _, v in values]
    overall_med = median(all_laeq)
    target = _hour_key(start_local)
    slot = [v for ts, v in values if _hour_key(ts) == target]
    # Same clock hour, any weekday, if weekday-hour is sparse
    if len(slot) < MIN_HOUR_SAMPLES:
        slot = [v for ts, v in values if ts.hour == start_local.hour]

    if len(slot) < MIN_HOUR_SAMPLES:
        return {
            "level": "insufficient",
            "label": "Insufficient data",
            "title": "Insufficient historical data to generate a reliable noise warning.",
            "evidence_kind": "measured_historical",
            "sample_count": len(slot),
            "event_local": start_local.isoformat(),
            "overall_median_db": round(overall_med, 1),
        }

    slot_med = median(slot)
    slot_mean = sum(slot) / len(slot)
    slot_min, slot_max = min(slot), max(slot)
    lift = slot_med - overall_med
    elevated = [v for v in slot if v >= overall_med + 5]
    freq = len(elevated) / len(slot)

    by_hour: dict[int, list[float]] = {}
    for ts, v in values:
        by_hour.setdefault(ts.hour, []).append(v)
    peak_hours = []
    for hour, vals in sorted(by_hour.items()):
        if len(vals) < MIN_HOUR_SAMPLES:
            continue
        if median(vals) >= overall_med + 4:
            peak_hours.append(hour)

    typical_period = None
    if peak_hours:
        typical_period = f"{peak_hours[0]:02d}:00–{(peak_hours[-1] + 1) % 24:02d}:00 Europe/Dublin"

    if lift >= 8 and freq >= 0.5 and len(slot) >= 6:
        level = "recurring"
        label = "Recurring Noise Pattern"
        pattern = "Recurring elevation at this clock hour in recent measurements"
        confidence = "medium"
    elif lift >= 3.5 or freq >= 0.35:
        level = "potential"
        label = "Potential Noise"
        pattern = "Some elevation at this clock hour versus this station’s recent median"
        confidence = "low"
    else:
        level = "normal"
        label = "Normal"
        pattern = "No meaningful recurring elevation detected at this clock hour"
        confidence = "medium"

    return {
        "level": level,
        "label": label,
        "pattern": pattern,
        "confidence": confidence,
        "evidence_kind": "measured_historical",
        "inference": "Compared this event’s local hour to recent hourly averages at the matched station. This is not a forecast of a future dB value.",
        "event_local": start_local.isoformat(),
        "event_hour": start_local.hour,
        "sample_count": len(slot),
        "lookback_samples": len(values),
        "slot_median_db": round(slot_med, 1),
        "slot_mean_db": round(slot_mean, 1),
        "slot_range_db": [round(slot_min, 1), round(slot_max, 1)],
        "overall_median_db": round(overall_med, 1),
        "lift_db": round(lift, 1),
        "elevated_frequency": round(freq, 2),
        "typical_period": typical_period,
        "unit": "dB(A) LAeq hourly",
    }


def generate_noise_alert(event: dict[str, Any], match: dict[str, Any], analysis: dict[str, Any]) -> dict[str, Any]:
    name = event.get("name") or "Event"
    loc = event.get("location") or "Unknown location"
    when = event.get("start")
    level = analysis.get("level")
    mon = match.get("monitor") or {}
    station = mon.get("location") or mon.get("label")

    if not match.get("matched"):
        return {
            "level": "unmatched",
            "badge": "Unable to match",
            "headline": f"{name}",
            "body": "Unable to match this calendar location to a Dublin Sonitus noise station. No warning was assumed.",
            "actions": [],
            "notify": False,
        }

    if level == "insufficient":
        return {
            "level": "insufficient",
            "badge": "Insufficient data",
            "headline": name,
            "body": "Insufficient historical data to generate a reliable noise warning.",
            "station": station,
            "actions": [],
            "notify": False,
            "analysis": analysis,
        }

    if level == "normal":
        return {
            "level": "normal",
            "badge": "Normal",
            "headline": name,
            "body": (
                f"You have an upcoming event at {loc}. "
                "Historical hourly averages at the matched station do not show a meaningful recurring elevation at this clock hour. "
                "This is not a guarantee of future quiet."
            ),
            "station": station,
            "pattern": analysis.get("pattern"),
            "confidence": analysis.get("confidence"),
            "typical_period": analysis.get("typical_period"),
            "actions": [],
            "notify": False,
            "analysis": analysis,
        }

    if level == "recurring":
        body = (
            f"You have an upcoming event at {loc} ({when}). "
            "Historical data indicates elevated environmental noise is common around this location during this time period. "
            "This is a pattern inferred from past hourly LAeq, not a prediction of a specific future decibel value."
        )
        actions = [
            "Consider arriving before the recurring peak period (recommendation, not a guarantee).",
            "If you need quieter conditions, check a nearby alternative place on the map.",
        ]
        notify = True
        notice = "You have an upcoming event in an area with a recurring elevated-noise pattern."
    else:
        body = (
            f"You have an upcoming event at {loc} ({when}). "
            "Historical measurements show some elevation around this clock hour at the matched station. "
            "Evidence is limited; this is not a forecast of a specific dB level."
        )
        actions = [
            "If quiet matters, allow extra time or pick a calmer nearby station from the map.",
        ]
        notify = True
        notice = "You have an upcoming event near a location with some historical elevation at this hour."

    return {
        "level": level,
        "badge": analysis.get("label"),
        "headline": name,
        "body": body,
        "station": station,
        "pattern": analysis.get("pattern"),
        "confidence": analysis.get("confidence"),
        "typical_period": analysis.get("typical_period"),
        "actions": actions,
        "notify": notify,
        "notice": notice,
        "analysis": analysis,
    }


def process_events(
    events: list[dict[str, Any]],
    monitors: list[dict[str, Any]],
    fetch_hourly: Callable[[str, datetime, datetime], list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    out = []
    now = datetime.now(DUBLIN)
    start_lookback = (now - timedelta(days=LOOKBACK_DAYS)).date()
    end_lookback = now.date()
    for event in events:
        loc = event.get("location")
        usable = bool(loc and str(loc).strip())
        if not usable:
            continue
        match = match_location_to_noise_data(loc, monitors)
        hourly: list[dict[str, Any]] = []
        if match.get("matched"):
            serial = match["monitor"]["serial_number"]
            try:
                hourly = fetch_hourly(serial, start_lookback, end_lookback)
            except Exception as exc:  # noqa: BLE001 — surface as insufficient, don't crash demo
                hourly = []
                match = {**match, "fetch_error": str(exc)}
        analysis = (
            analyze_noise_for_event(event, hourly)
            if match.get("matched")
            else {
                "level": "unmatched",
                "title": "Unable to match location",
                "evidence_kind": "none",
            }
        )
        alert = generate_noise_alert(event, match, analysis)
        out.append(
            {
                "event": event,
                "match": {
                    "matched": match.get("matched"),
                    "reason": match.get("reason"),
                    "score": match.get("score"),
                    "hint": match.get("hint"),
                    "station": (match.get("monitor") or {}).get("location"),
                    "serial_number": (match.get("monitor") or {}).get("serial_number"),
                    "kind": "measured_historical" if match.get("matched") else "none",
                },
                "alert": alert,
            }
        )
    return out


def demo_events() -> list[dict[str, Any]]:
    """Upcoming events at real Dublin noise stations."""
    today = datetime.now(DUBLIN).date()
    day = today + timedelta(days=1)

    def iso(hour: int, minute: int = 0) -> str:
        dt = datetime(day.year, day.month, day.day, hour, minute, tzinfo=DUBLIN)
        return dt.isoformat()

    pool = [
        ("School drop-off", "Drumcondra Library", 8, 0, 9, 0),
        ("Morning run", "Bull Island", 7, 15, 8, 0),
        ("Coffee", "Chancery Park", 10, 30, 11, 15),
        ("Studio visit", "Ringsend Sports Centre", 14, 0, 15, 30),
        ("GP appointment", "Raheny", 16, 0, 16, 45),
        ("Match", "Mellows Park", 18, 0, 19, 30),
        ("Dinner", "Strand Road", 19, 30, 21, 0),
        ("Walk home", "Walkinstown", 21, 0, 21, 40),
        ("Late study", "Blessington Basin", 20, 0, 21, 0),
        ("Market stall", "Dolphins Barn", 11, 0, 12, 30),
    ]
    picked = [pool[0], pool[2], pool[4], pool[5], pool[6]]
    events = []
    for i, (name, location, sh, sm, eh, em) in enumerate(picked):
        events.append(
            {
                "id": f"event-{i}-{sh}{sm}",
                "name": name,
                "start": iso(sh, sm),
                "end": iso(eh, em),
                "location": location,
                "all_day": False,
                "source": "upcoming",
            }
        )
    return events
