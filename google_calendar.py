"""Google Calendar read-only access.

Verified against:
  https://developers.google.com/identity/protocols/oauth2/web-server
  https://developers.google.com/workspace/calendar/api/v3/reference/events/list
  https://developers.google.com/identity/protocols/oauth2/scopes

Scope used (minimum that can list events):
  https://www.googleapis.com/auth/calendar.events.readonly
  ("View events on all your calendars")

Not requested: calendar, calendar.events (those allow write).
"""

from __future__ import annotations

import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode

import requests
from dotenv import load_dotenv

load_dotenv()

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
EVENTS_URL = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
READONLY_SCOPE = "https://www.googleapis.com/auth/calendar.events.readonly"


class GoogleCalendarError(RuntimeError):
    pass


def _settings() -> dict[str, str]:
    return {
        "client_id": (os.getenv("GOOGLE_CLIENT_ID") or "").strip(),
        "client_secret": (os.getenv("GOOGLE_CLIENT_SECRET") or "").strip(),
        "redirect_uri": (
            os.getenv("GOOGLE_REDIRECT_URI") or "http://127.0.0.1:8000/auth/google/callback"
        ).strip(),
    }


def google_oauth_configured() -> bool:
    s = _settings()
    return bool(s["client_id"] and s["client_secret"])


def authenticate_google_calendar() -> tuple[str, str]:
    """Return (authorization_url, state) for the OAuth consent redirect."""
    s = _settings()
    if not s["client_id"] or not s["client_secret"]:
        raise GoogleCalendarError(
            "GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET are not set. Use Demo mode, or add them to .env."
        )
    state = secrets.token_urlsafe(24)
    params = {
        "client_id": s["client_id"],
        "redirect_uri": s["redirect_uri"],
        "response_type": "code",
        "scope": READONLY_SCOPE,
        "access_type": "offline",
        "include_granted_scopes": "true",
        "prompt": "consent",
        "state": state,
    }
    return f"{AUTH_URL}?{urlencode(params)}", state


def exchange_code_for_tokens(code: str) -> dict[str, Any]:
    s = _settings()
    response = requests.post(
        TOKEN_URL,
        data={
            "client_id": s["client_id"],
            "client_secret": s["client_secret"],
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": s["redirect_uri"],
        },
        timeout=30,
    )
    if response.status_code >= 400:
        raise GoogleCalendarError(f"Google token exchange failed ({response.status_code}): {response.text[:300]}")
    body = response.json()
    expires_in = int(body.get("expires_in") or 3600)
    body["expires_at"] = (datetime.now(timezone.utc) + timedelta(seconds=expires_in - 60)).isoformat()
    return body


def refresh_access_token(refresh_token: str) -> dict[str, Any]:
    s = _settings()
    response = requests.post(
        TOKEN_URL,
        data={
            "client_id": s["client_id"],
            "client_secret": s["client_secret"],
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
        timeout=30,
    )
    if response.status_code >= 400:
        raise GoogleCalendarError(f"Google token refresh failed ({response.status_code}): {response.text[:300]}")
    return response.json()


def get_upcoming_events(access_token: str, days: int = 14) -> list[dict[str, Any]]:
    """List upcoming events on the primary calendar (read-only)."""
    now = datetime.now(timezone.utc)
    params = {
        "timeMin": now.isoformat().replace("+00:00", "Z"),
        "timeMax": (now + timedelta(days=days)).isoformat().replace("+00:00", "Z"),
        "singleEvents": "true",
        "orderBy": "startTime",
        "maxResults": 40,
        "eventTypes": "default",
    }
    response = requests.get(
        EVENTS_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        params=params,
        timeout=30,
    )
    if response.status_code >= 400:
        raise GoogleCalendarError(f"Calendar events.list failed ({response.status_code}): {response.text[:300]}")
    items = response.json().get("items") or []
    events = []
    for item in items:
        parsed = parse_calendar_event(item)
        if parsed:
            events.append(parsed)
    return events


def parse_calendar_event(item: dict[str, Any]) -> dict[str, Any] | None:
    start = item.get("start") or {}
    end = item.get("end") or {}
    start_raw = start.get("dateTime") or start.get("date")
    if not start_raw:
        return None
    return {
        "id": item.get("id"),
        "name": item.get("summary") or "(No title)",
        "start": start_raw,
        "end": end.get("dateTime") or end.get("date"),
        "location": extract_event_location(item),
        "time_zone": start.get("timeZone") or end.get("timeZone"),
        "all_day": "date" in start and "dateTime" not in start,
        "source": "google",
    }


def extract_event_location(item: dict[str, Any]) -> str | None:
    loc = item.get("location")
    if loc is None:
        return None
    text = str(loc).strip()
    return text or None
