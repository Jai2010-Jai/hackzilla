"""Groq summaries for one Dublin noise station.

The model only sees hourly averages we compute from that station's
five-minute LAeq. It must not invent times, places, or colour bands.
"""

from __future__ import annotations

import json
import os
import re
from collections import defaultdict
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

load_dotenv()

GROQ_CHAT_URL = "https://api.groq.com/openai/v1/chat/completions"
DEFAULT_MODEL = "openai/gpt-oss-20b"
DUBLIN = ZoneInfo("Europe/Dublin")


class GroqError(RuntimeError):
    pass


def _extract_json(text: str) -> Any:
    raw = text.strip()
    fenced = re.search(r"```(?:json)?\s*([\s\S]*?)```", raw)
    if fenced:
        raw = fenced.group(1).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}", raw)
        if not match:
            raise
        return json.loads(match.group(0))


def _parse_ts(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=DUBLIN)
    return dt.astimezone(DUBLIN)


def hourly_means(readings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[str, list[float]] = defaultdict(list)
    for row in readings:
        laeq = row.get("value")
        if laeq is None:
            laeq = row.get("laeq")
        if laeq is None:
            continue
        try:
            val = float(laeq)
        except (TypeError, ValueError):
            continue
        local = _parse_ts(row.get("timestamp"))
        if local is None:
            continue
        key = local.strftime("%Y-%m-%d %H:00")
        buckets[key].append(val)
    hours = []
    for hour, vals in sorted(buckets.items()):
        hours.append(
            {
                "hour": hour,
                "mean_db": round(sum(vals) / len(vals), 1),
                "max_db": round(max(vals), 1),
                "samples": len(vals),
            }
        )
    return hours


def _chat(system: str, user: str, max_tokens: int = 700) -> dict[str, Any]:
    key = (os.getenv("GROQ_API_KEY") or "").strip()
    if not key:
        raise GroqError("GROQ_API_KEY is not set on the server")
    model = (os.getenv("GROQ_MODEL") or DEFAULT_MODEL).strip()
    response = requests.post(
        GROQ_CHAT_URL,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "temperature": 0.2,
            "max_completion_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        },
        timeout=60,
    )
    if response.status_code >= 400:
        raise GroqError(f"Groq request failed ({response.status_code}): {response.text[:400]}")
    body = response.json()
    parsed = _extract_json(body["choices"][0]["message"]["content"])
    if not isinstance(parsed, dict):
        raise GroqError("Groq did not return a JSON object")
    parsed["_model"] = model
    parsed["_usage"] = body.get("usage") or {}
    return parsed


def _hour_num(hour_key: str) -> int | None:
    try:
        return int(hour_key.split()[1][:2])
    except (IndexError, ValueError):
        return None


def _mean(vals: list[float]) -> float | None:
    if not vals:
        return None
    return round(sum(vals) / len(vals), 1)


def place_facts(hours: list[dict[str, Any]], stats: dict[str, Any] | None) -> dict[str, Any]:
    ranked = sorted(hours, key=lambda h: h["mean_db"])
    quiet = ranked[0]
    loud = ranked[-1]
    parts_def = (
        ("Morning", "6am–noon", range(6, 12)),
        ("Afternoon", "noon–6pm", range(12, 18)),
        ("Evening", "6pm–10pm", range(18, 22)),
        ("Night", "10pm–6am", list(range(22, 24)) + list(range(0, 6))),
    )
    parts = []
    for label, window, hours_set in parts_def:
        vals = [h["mean_db"] for h in hours if _hour_num(h["hour"]) in hours_set]
        if not vals:
            continue
        parts.append(
            {
                "label": label,
                "window": window,
                "mean_db": _mean(vals),
                "hours": len(vals),
            }
        )
    return {
        "loudest_hour": loud["hour"],
        "loudest_db": loud["mean_db"],
        "quietest_hour": quiet["hour"],
        "quietest_db": quiet["mean_db"],
        "swing_db": round(loud["mean_db"] - quiet["mean_db"], 1),
        "day_mean": (stats or {}).get("mean"),
        "day_min": (stats or {}).get("min"),
        "day_max": (stats or {}).get("max"),
        "hours_used": len(hours),
        "parts": parts,
        "top_loud": list(reversed(ranked[-3:])),
        "top_quiet": ranked[:3],
        "high_volume": bool(
            (loud["mean_db"] >= 65)
            or ((stats or {}).get("mean") is not None and float((stats or {})["mean"]) >= 65)
            or ((stats or {}).get("max") is not None and float((stats or {})["max"]) >= 80)
        ),
    }


def _fallback_mitigation(facts: dict[str, Any], place: str | None) -> list[str]:
    spot = place or "this place"
    quiet = facts.get("quietest_hour") or "a quieter hour"
    loud = facts.get("loudest_hour") or "the noisiest hour"
    return [
        f"Move the visit toward {quiet} if you can — that’s the calmer window in this reading.",
        f"Avoid lingering around {loud}; that’s when {spot} is usually at its loudest.",
        "Keep the stop short, and stand back from the kerb or traffic if you’re outdoors.",
        "If you have to stay through a loud hour, ear protection helps more than waiting it out.",
    ]


def generate_place_brief(payload: dict[str, Any]) -> dict[str, Any]:
    readings = payload.get("readings") or []
    if not isinstance(readings, list) or not readings:
        raise GroqError("No readings for this place")
    hours = hourly_means(readings)
    if not hours:
        raise GroqError("Could not group this place into hours")

    facts = place_facts(hours, payload.get("stats") if isinstance(payload.get("stats"), dict) else None)
    grounded = {
        "place": payload.get("location"),
        "dates": {"from": payload.get("start"), "to": payload.get("end")},
        "clock": "Europe/Dublin",
        "metric": payload.get("metric_label") or "reading",
        "kind": payload.get("kind"),
        "unit": payload.get("unit") or "as reported",
        "facts": facts,
        "by_hour": hours,
    }
    kind = payload.get("kind") or "noise"
    high = bool(facts.get("high_volume"))
    if kind == "noise":
        system = (
            "You help someone visiting one Dublin place. Plain English. "
            "Do not mention map colours, colour bands, LAeq, or legal limits. "
            "Say 'average noise' and 'decibels'. "
            "Only use hours in the JSON. Do not invent times. "
            "Reply JSON only with: "
            "summary (3 or 4 sentences), "
            "loudest (which hours were noisiest and roughly how loud), "
            "go_when (best calmer window to visit), "
            "avoid (hours to skip if they want quiet), "
            "expect (what the day generally felt like), "
            "tips (array of 3 short practical tips grounded in these hours), "
            "mitigation (array of 4 short actions if facts.high_volume is true; "
            "otherwise an empty array). "
            "Mitigation is for a visitor facing very loud conditions at this same place: "
            "shift to the quieter hours in the JSON, keep the visit short, stand back from the road, "
            "use ear protection, duck indoors if they can. "
            "Do not invent other place names. No medical or legal claims."
        )
    else:
        system = (
            "You help someone reading one Dublin air (or other) monitor. Plain English. "
            "Do not mention map colours or invent pollutants, units, or legal limits. "
            "Use only the metric name in the JSON. "
            "Only use hours in the JSON. Do not invent times. "
            "Reply JSON only with: "
            "summary (3 or 4 sentences), "
            "loudest (which hours were highest), "
            "go_when (calmer / lower hours if they want cleaner air), "
            "avoid (higher hours), "
            "expect (what the day generally looked like), "
            "tips (array of 3 short tips grounded in these hours), "
            "mitigation (empty array)."
        )
    parsed = _chat(system, json.dumps(grounded, ensure_ascii=False), max_tokens=2500)
    summary = str(parsed.get("summary") or "").strip()
    loudest = str(parsed.get("loudest") or "").strip()
    go_when = str(parsed.get("go_when") or "").strip()
    avoid = str(parsed.get("avoid") or "").strip()
    expect = str(parsed.get("expect") or "").strip()
    tips = parsed.get("tips") if isinstance(parsed.get("tips"), list) else []
    tips = [str(t).strip() for t in tips if str(t).strip()][:4]
    mitigation = parsed.get("mitigation") if isinstance(parsed.get("mitigation"), list) else []
    mitigation = [str(t).strip() for t in mitigation if str(t).strip()][:5]
    if high and not mitigation:
        mitigation = _fallback_mitigation(facts, payload.get("location"))
    if not high:
        mitigation = []
    if not (summary and loudest and go_when):
        raise GroqError("Groq returned an incomplete place summary")
    return {
        "model": parsed.get("_model"),
        "place": payload.get("location"),
        "start": payload.get("start"),
        "end": payload.get("end"),
        "facts": facts,
        "summary": summary,
        "loudest": loudest,
        "go_when": go_when,
        "avoid": avoid,
        "expect": expect,
        "tips": tips,
        "mitigation": mitigation,
        "high_volume": high,
        "usage": parsed.get("_usage"),
    }


def _chat_messages(system: str, messages: list[dict[str, str]], max_tokens: int = 500) -> tuple[str, str, dict[str, Any]]:
    key = (os.getenv("GROQ_API_KEY") or "").strip()
    if not key:
        raise GroqError("GROQ_API_KEY is not set on the server")
    model = (os.getenv("GROQ_MODEL") or DEFAULT_MODEL).strip()
    payload_messages = [{"role": "system", "content": system}, *messages]
    response = requests.post(
        GROQ_CHAT_URL,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "temperature": 0.3,
            "max_completion_tokens": max_tokens,
            "messages": payload_messages,
        },
        timeout=60,
    )
    if response.status_code >= 400:
        raise GroqError(f"Groq request failed ({response.status_code}): {response.text[:400]}")
    body = response.json()
    text = str(body["choices"][0]["message"]["content"] or "").strip()
    if not text:
        raise GroqError("Groq returned an empty reply")
    return text, model, body.get("usage") or {}


def answer_noise_chat(
    question: str,
    stations: list[dict[str, Any]] | None = None,
    history: list[dict[str, Any]] | None = None,
    selected: dict[str, Any] | None = None,
) -> dict[str, Any]:
    q = str(question or "").strip()
    if len(q) < 2:
        raise GroqError("Ask a short question about a Dublin place.")
    if len(q) > 400:
        q = q[:400]
    slim_stations = []
    for row in stations or []:
        loc = str(row.get("location") or "").strip()
        if not loc:
            continue
        slim_stations.append(
            {
                "location": loc,
                "mean_db": row.get("mean"),
                "min_db": row.get("min"),
                "max_db": row.get("max"),
            }
        )
        if len(slim_stations) >= 24:
            break
    turns: list[dict[str, str]] = []
    for item in (history or [])[-8:]:
        role = item.get("role")
        content = str(item.get("content") or "").strip()
        if role not in ("user", "assistant") or not content:
            continue
        turns.append({"role": role, "content": content[:800]})
    turns.append({"role": "user", "content": q})
    grounded = {
        "clock": "Europe/Dublin",
        "stations": slim_stations,
        "selected_place": selected,
        "guide": {
            "low_db": "20–60 quiet to everyday city",
            "medium_db": "60–80 busy street",
            "high_db": "80+ very loud",
            "loud_band": "mean ≥ 65 is loud on this dashboard",
        },
    }
    system = (
        "You help someone visiting Dublin decide how loud a place usually is, "
        "and whether they should go. Plain English. Short answers. "
        "Use only the station list and selected_place in the JSON. "
        "If they name a place that is not in the list, say you have no monitor there. "
        "Say 'decibels', not LAeq. Do not invent times, other cities, or legal limits. "
        "If they ask should I go: give a clear take (go / go but keep it short / skip if you want quiet) "
        "from the numbers. Add one practical tip if it is loud (≥ 65 dB mean). "
        "This is a usual pattern, not tomorrow’s exact sound. Reply as chat text, not JSON."
    )
    user = q + "\n\nContext JSON:\n" + json.dumps(grounded, ensure_ascii=False)
    # Put context on the last user turn
    turns[-1] = {"role": "user", "content": user}
    text, model, usage = _chat_messages(system, turns, max_tokens=450)
    return {"reply": text, "model": model, "usage": usage}
