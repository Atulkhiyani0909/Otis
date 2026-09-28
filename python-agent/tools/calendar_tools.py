import uuid
from datetime import datetime, timezone
from typing import Optional

from googleapiclient.discovery import build
from langchain_core.tools import tool

from tools.auth_helper import build_google_credentials

USER_TIMEZONE = "Asia/Kolkata"
DEFAULT_OFFSET = "+05:30"


def get_calendar_service(creds_data: dict):
    creds = build_google_credentials(creds_data)
    return build("calendar", "v3", credentials=creds, static_discovery=False)


def normalize_iso(ts: str) -> str:
    """Append the default offset if the timestamp has no timezone info."""
    ts = ts.strip()
    if not ts.endswith("Z") and not ("+" in ts[10:] or "-" in ts[10:]):
        return f"{ts}{DEFAULT_OFFSET}"
    return ts


@tool
def list_upcoming_events(max_results: int = 5) -> str:
    """Lists upcoming events on the user's primary calendar from now onwards.
    Each line includes the event ID, which is needed to update or delete an event."""
    from agent import get_current_google_tokens
    tokens = get_current_google_tokens()
    if not tokens:
        return "Error: No Google authentication tokens available."

    try:
        service = get_calendar_service(tokens)
        now = datetime.now(timezone.utc).isoformat()

        events_result = service.events().list(
            calendarId="primary",
            timeMin=now,
            maxResults=max_results,
            singleEvents=True,
            orderBy="startTime"
        ).execute()
        events = events_result.get("items", [])

        if not events:
            return "No upcoming events found on your calendar."

        formatted = []
        for event in events:
            start = event["start"].get("dateTime", event["start"].get("date"))
            summary = event.get("summary", "Untitled Event")
            formatted.append(f"• {start} — {summary} (ID: {event['id']})")

        return "\n".join(formatted)
    except Exception as e:
        return f"Failed to list calendar events: {str(e)}"


@tool
def create_calendar_event(
    summary: str,
    start_time_iso: str,
    end_time_iso: str,
    description: str = "",
    attendee_email: str = "",
    create_meet_link: bool = False
) -> str:
    """
    Creates an event on the user's primary Google Calendar with optional Google Meet link and attendee invites.
    Args:
        summary: Title of the event (e.g. 'Project Strategy Sync').
        start_time_iso: ISO 8601 start timestamp with timezone (e.g., '2026-09-26T15:00:00+05:30').
        end_time_iso: ISO 8601 end timestamp with timezone (e.g., '2026-09-26T15:30:00+05:30').
        description: Optional notes, agenda, or location.
        attendee_email: Optional email of a person to invite to the meeting.
        create_meet_link: Set to True to generate and attach an official Google Meet video conference.
    """
    from agent import get_current_google_tokens
    tokens = get_current_google_tokens()
    if not tokens:
        return "Error: No Google authentication tokens available."

    try:
        service = get_calendar_service(tokens)

        clean_start = normalize_iso(start_time_iso)
        clean_end = normalize_iso(end_time_iso)

        event_body = {
            "summary": summary,
            "description": description,
            "start": {"dateTime": clean_start, "timeZone": USER_TIMEZONE},
            "end": {"dateTime": clean_end, "timeZone": USER_TIMEZONE},
        }

        if attendee_email:
            event_body["attendees"] = [{"email": attendee_email.strip()}]

        conference_version = 0
        if create_meet_link:
            conference_version = 1
            event_body["conferenceData"] = {
                "createRequest": {
                    "requestId": str(uuid.uuid4()),
                    "conferenceSolutionKey": {"type": "hangoutsMeet"}
                }
            }

        created = service.events().insert(
            calendarId="primary",
            body=event_body,
            conferenceDataVersion=conference_version,
            sendUpdates="all" if attendee_email else "none"
        ).execute()

        meet_url = created.get("hangoutLink", "No Meet link generated")

        response_msg = f"Event created: '{summary}' from {clean_start} to {clean_end}."
        if create_meet_link:
            response_msg += f"\n📹 Google Meet Link: {meet_url}"
        if attendee_email:
            response_msg += f"\n✉️ Invite sent to: {attendee_email}"

        return response_msg

    except Exception as e:
        return f"Failed to create event: {str(e)}"


@tool
def update_calendar_event(
    event_id: str,
    summary: Optional[str] = None,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    description: Optional[str] = None,
    location: Optional[str] = None,
    calendar_id: str = "primary"
) -> str:
    """
    Updates an existing Google Calendar event's title, time, location, or description.

    Args:
        event_id: The unique ID of the event to update (shown as ID in list_upcoming_events output).
        summary: New title of the event (optional).
        start_time: New start time in ISO-8601 format (e.g. 2026-09-28T16:00:00+05:30) (optional).
        end_time: New end time in ISO-8601 format (e.g. 2026-09-28T17:00:00+05:30) (optional).
        description: New description/notes for the event (optional).
        location: New location or link for the event (optional).
        calendar_id: The calendar ID (default: 'primary').
    """
    from agent import get_current_google_tokens
    tokens = get_current_google_tokens()
    if not tokens:
        return "Error: No Google authentication tokens available."

    try:
        service = get_calendar_service(tokens)

        # Build a partial body with only the fields being changed
        body = {}
        if summary:
            body["summary"] = summary
        if description is not None:
            body["description"] = description
        if location is not None:
            body["location"] = location
        if start_time:
            body["start"] = {
                "dateTime": normalize_iso(start_time),
                "timeZone": USER_TIMEZONE,
            }
        if end_time:
            body["end"] = {
                "dateTime": normalize_iso(end_time),
                "timeZone": USER_TIMEZONE,
            }

        if not body:
            return "No fields provided to update."

        updated_event = service.events().patch(
            calendarId=calendar_id,
            eventId=event_id,
            body=body,
            sendUpdates="all"
        ).execute()

        event_title = updated_event.get("summary", "Event")
        event_link = updated_event.get("htmlLink", "")
        return f"✅ Updated calendar event: **{event_title}**\n🔗 [Open in Calendar]({event_link})"
    except Exception as e:
        return f"Failed to update calendar event: {str(e)}"


@tool
def delete_calendar_event(
    event_id: str,
    calendar_id: str = "primary"
) -> str:
    """
    Deletes an event from Google Calendar by its event ID.

    Args:
        event_id: The unique ID of the event to delete (shown as ID in list_upcoming_events output).
        calendar_id: The calendar ID (default: 'primary').
    """
    from agent import get_current_google_tokens
    tokens = get_current_google_tokens()
    if not tokens:
        return "Error: No Google authentication tokens available."

    try:
        service = get_calendar_service(tokens)
        service.events().delete(
            calendarId=calendar_id,
            eventId=event_id,
            sendUpdates="all"
        ).execute()
        return f"🗑️ Calendar event with ID `{event_id}` was successfully removed."
    except Exception as e:
        return f"Failed to delete calendar event: {str(e)}"