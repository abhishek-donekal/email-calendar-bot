"""Email-to-calendar scheduling bot.

Reads unread Gmail messages, asks Gemini to extract a calendar event from each,
inserts any event it finds into the primary Google Calendar, then marks the
message read (and labels it `Scheduled` if that label exists).

Runs locally (OAuth browser flow, token.json) or headless on GitHub Actions
(GOOGLE_REFRESH_TOKEN / GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET env vars).

Usage:
    python scheduler_bot.py            # process mail
    python scheduler_bot.py --dry-run  # extract and log only; change nothing
"""

from __future__ import annotations

import argparse
import base64
import logging
import os
import sys
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from dateutil import parser as date_parser
from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from pydantic import BaseModel, Field

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/calendar.events",
]
TOKEN_URI = "https://oauth2.googleapis.com/token"
TOKEN_FILE = "token.json"
CREDENTIALS_FILE = "credentials.json"

GEMINI_MODEL = "gemini-2.5-flash"
GMAIL_QUERY = "is:unread -label:Scheduled"
SCHEDULED_LABEL_NAME = "Scheduled"
MAX_BODY_CHARS = 8000

log = logging.getLogger("scheduler_bot")


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def get_services():
    """Return (gmail, calendar) API clients.

    Order of precedence:
      1. GOOGLE_REFRESH_TOKEN + GOOGLE_CLIENT_ID + GOOGLE_CLIENT_SECRET env vars (headless / CI)
      2. A valid local token.json
      3. Interactive OAuth flow from credentials.json, which writes token.json
    """
    refresh_token = os.getenv("GOOGLE_REFRESH_TOKEN")
    client_id = os.getenv("GOOGLE_CLIENT_ID")
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET")

    creds: Optional[Credentials] = None

    if refresh_token and client_id and client_secret:
        log.info("Using OAuth credentials from environment variables.")
        creds = Credentials(
            token=None,
            refresh_token=refresh_token,
            client_id=client_id,
            client_secret=client_secret,
            token_uri=TOKEN_URI,
            scopes=SCOPES,
        )
        creds.refresh(Request())
    else:
        if os.path.exists(TOKEN_FILE):
            creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                except Exception as exc:  # revoked or scopes changed
                    log.warning("Could not refresh %s (%s); re-running OAuth flow.", TOKEN_FILE, exc)
                    creds = None

            if not creds or not creds.valid:
                if not os.path.exists(CREDENTIALS_FILE):
                    raise FileNotFoundError(
                        f"No GOOGLE_* env vars, no valid {TOKEN_FILE}, and no {CREDENTIALS_FILE}. "
                        "See README.md for setup."
                    )
                flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
                creds = flow.run_local_server(port=0)

            with open(TOKEN_FILE, "w") as fh:
                fh.write(creds.to_json())
            log.info("Saved credentials to %s.", TOKEN_FILE)

    gmail = build("gmail", "v1", credentials=creds, cache_discovery=False)
    calendar = build("calendar", "v3", credentials=creds, cache_discovery=False)
    return gmail, calendar


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

class CalendarEventExtraction(BaseModel):
    has_event: bool = Field(description="True only if the email proposes or confirms a specific event with a date/time.")
    title: Optional[str] = Field(default=None, description="Short event title.")
    start_iso: Optional[str] = Field(default=None, description="Start as ISO 8601 with UTC offset, e.g. 2026-10-30T15:00:00-04:00.")
    end_iso: Optional[str] = Field(default=None, description="End as ISO 8601 with UTC offset. If unspecified, 1 hour after start.")
    location: Optional[str] = Field(default=None, description="Physical address or meeting link, if any.")
    description: Optional[str] = Field(default=None, description="One or two sentence summary of the event.")


def get_timezone() -> ZoneInfo:
    """Timezone used to resolve relative dates. GitHub runners are UTC, so set TIMEZONE."""
    name = os.getenv("TIMEZONE", "UTC")
    try:
        return ZoneInfo(name)
    except Exception:
        log.warning("Unknown TIMEZONE %r; falling back to UTC.", name)
        return ZoneInfo("UTC")


def build_prompt(subject: str, sender: str, body: str, now: datetime) -> str:
    reference = now.strftime("%A, %B %d, %Y at %H:%M %Z (UTC%z)")
    return f"""You extract calendar events from emails.

Reference time (now): {reference}
Resolve relative dates ("tomorrow", "this Friday", "next Tuesday") against this reference time.
Interpret times without a timezone in {now.tzinfo}. Return start_iso and end_iso as ISO 8601
with a UTC offset. If no end time is given, set end_iso to 1 hour after start_iso.

Set has_event to false for newsletters, receipts, marketing, or emails without a concrete
date and time for something the recipient would attend. Do not invent dates.

From: {sender}
Subject: {subject}

Body:
{body}
"""


def extract_event_from_email(subject: str, sender: str, body: str, client=None) -> CalendarEventExtraction:
    from google import genai
    from google.genai import types

    client = client or genai.Client()  # reads GEMINI_API_KEY
    prompt = build_prompt(subject, sender, body, datetime.now(get_timezone()))
    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=CalendarEventExtraction,
            temperature=0,
        ),
    )
    if isinstance(response.parsed, CalendarEventExtraction):
        return response.parsed
    return CalendarEventExtraction.model_validate_json(response.text)


def normalize_times(event: CalendarEventExtraction, tz: ZoneInfo) -> Optional[tuple[datetime, datetime]]:
    """Parse start/end; default end to start + 1h. Returns None if start is missing or invalid."""
    if not event.start_iso:
        return None
    try:
        start = date_parser.isoparse(event.start_iso)
    except (ValueError, TypeError):
        return None
    if start.tzinfo is None:
        start = start.replace(tzinfo=tz)

    end = None
    if event.end_iso:
        try:
            end = date_parser.isoparse(event.end_iso)
            if end.tzinfo is None:
                end = end.replace(tzinfo=tz)
        except (ValueError, TypeError):
            end = None
    if end is None or end <= start:
        end = start + timedelta(hours=1)
    return start, end


# ---------------------------------------------------------------------------
# Calendar
# ---------------------------------------------------------------------------

def create_calendar_event(calendar, event: CalendarEventExtraction, start: datetime, end: datetime, tz: ZoneInfo) -> dict:
    body = {
        "summary": event.title or "(untitled event)",
        "start": {"dateTime": start.isoformat(), "timeZone": str(tz)},
        "end": {"dateTime": end.isoformat(), "timeZone": str(tz)},
    }
    if event.location:
        body["location"] = event.location
    if event.description:
        body["description"] = event.description
    return calendar.events().insert(calendarId="primary", body=body).execute()


# ---------------------------------------------------------------------------
# Gmail helpers
# ---------------------------------------------------------------------------

def get_header(headers: list[dict], name: str) -> str:
    for h in headers:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def _decode(data: str) -> str:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", errors="replace")


def find_plain_text(payload: dict) -> Optional[str]:
    """Depth-first search of the MIME tree for the first text/plain part with data."""
    if payload.get("mimeType") == "text/plain" and payload.get("body", {}).get("data"):
        return _decode(payload["body"]["data"])
    for part in payload.get("parts", []) or []:
        text = find_plain_text(part)
        if text:
            return text
    return None


def get_message_body(message: dict) -> str:
    text = find_plain_text(message.get("payload", {}))
    return (text or message.get("snippet", "")).strip()[:MAX_BODY_CHARS]


def get_label_id(gmail, name: str) -> Optional[str]:
    labels = gmail.users().labels().list(userId="me").execute().get("labels", [])
    for label in labels:
        if label.get("name", "").lower() == name.lower():
            return label["id"]
    return None


def list_unread_message_ids(gmail, max_messages: int) -> list[str]:
    ids: list[str] = []
    page_token = None
    while len(ids) < max_messages:
        resp = gmail.users().messages().list(
            userId="me", q=GMAIL_QUERY, maxResults=min(100, max_messages - len(ids)), pageToken=page_token
        ).execute()
        ids.extend(m["id"] for m in resp.get("messages", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return ids


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def run_scheduler_bot(dry_run: bool = False, max_messages: int = 25) -> dict:
    gmail, calendar = get_services()
    tz = get_timezone()

    scheduled_label_id = get_label_id(gmail, SCHEDULED_LABEL_NAME)
    if not scheduled_label_id:
        log.info("No '%s' label in Gmail; messages will only be marked read. "
                 "Create the label to have processed mail tagged.", SCHEDULED_LABEL_NAME)

    message_ids = list_unread_message_ids(gmail, max_messages)
    log.info("Found %d message(s) matching %r (cap %d).", len(message_ids), GMAIL_QUERY, max_messages)

    stats = {"seen": len(message_ids), "events_created": 0, "no_event": 0, "errors": 0}

    for msg_id in message_ids:
        try:
            message = gmail.users().messages().get(userId="me", id=msg_id, format="full").execute()
            headers = message.get("payload", {}).get("headers", [])
            subject = get_header(headers, "Subject") or "(no subject)"
            sender = get_header(headers, "From")
            body = get_message_body(message)

            event = extract_event_from_email(subject, sender, body)
            times = normalize_times(event, tz) if event.has_event else None

            if times:
                start, end = times
                if dry_run:
                    log.info("[dry-run] Would create %r %s -> %s (from %r)", event.title, start, end, subject)
                else:
                    created = create_calendar_event(calendar, event, start, end, tz)
                    log.info("Created event %r: %s", event.title, created.get("htmlLink"))
                stats["events_created"] += 1
            else:
                if event.has_event:
                    log.info("Event found but start_iso invalid (%r) in %r; skipping.", event.start_iso, subject)
                else:
                    log.info("No event in %r.", subject)
                stats["no_event"] += 1

            if dry_run:
                continue

            modify = {"removeLabelIds": ["UNREAD"]}
            if scheduled_label_id:
                modify["addLabelIds"] = [scheduled_label_id]
            gmail.users().messages().modify(userId="me", id=msg_id, body=modify).execute()
        except Exception:
            # Leave the message unread so the next run retries it.
            log.exception("Failed to process message %s; leaving it unread.", msg_id)
            stats["errors"] += 1

    log.info("Done: %s", stats)
    return stats


def main(argv: Optional[list[str]] = None) -> int:
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    ap = argparse.ArgumentParser(description="Turn emails into Google Calendar events.")
    ap.add_argument("--dry-run", action="store_true", help="Extract and log only; do not create events or modify mail.")
    ap.add_argument("--max", type=int, default=int(os.getenv("MAX_EMAILS", "25")), help="Max emails per run (default 25).")
    args = ap.parse_args(argv)

    if not os.getenv("GEMINI_API_KEY") and not os.getenv("GOOGLE_API_KEY"):
        log.error("GEMINI_API_KEY is not set.")
        return 1

    stats = run_scheduler_bot(dry_run=args.dry_run, max_messages=args.max)
    return 1 if stats["errors"] and stats["errors"] == stats["seen"] else 0


if __name__ == "__main__":
    sys.exit(main())
