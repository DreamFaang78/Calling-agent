import os
import datetime
import logging
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

logger = logging.getLogger("calendar-integration")
SCOPES = ["https://www.googleapis.com/auth/calendar.events"]

def _get_credentials():
    creds = None
    token_path = os.path.join(os.path.dirname(__file__), "token.json")
    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)
    else:
        # Fallback for Coolify deployment: load from database setting injected into env
        token_json_str = os.getenv("GOOGLE_OAUTH_TOKEN", "")
        if token_json_str:
            try:
                import json
                info = json.loads(token_json_str)
                creds = Credentials.from_authorized_user_info(info, SCOPES)
            except Exception as exc:
                logger.error("Failed to parse GOOGLE_OAUTH_TOKEN: %s", exc)

    # The minted access token expires in ~1 hour; without this refresh, every
    # calendar read/write silently no-ops once it lapses (creds.valid == False),
    # even though the long-lived refresh_token can mint a new one. Refresh it.
    if creds and not creds.valid and creds.expired and creds.refresh_token:
        try:
            from google.auth.transport.requests import Request
            creds.refresh(Request())
            logger.info("Refreshed expired Google Calendar access token.")
        except Exception as exc:
            logger.error("Failed to refresh Google OAuth token: %s", exc)

    return creds

async def check_google_calendar_availability(date_str: str, time_str: str, duration_minutes: int = 30) -> bool:
    """
    Check if a time slot is free on Google Calendar.
    date_str: YYYY-MM-DD
    time_str: HH:MM
    Returns True if the slot is FREE (or if calendar is not configured).
    Returns False if the slot is BUSY.
    """
    creds = _get_credentials()
    if not creds or not creds.valid:
        # If token.json is missing or expired, skip Google check and fallback to local DB
        logger.warning("No valid token.json found. Skipping Google Calendar check.")
        return True
        
    try:
        # Parse the requested date and time
        start_dt = datetime.datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
        end_dt = start_dt + datetime.timedelta(minutes=duration_minutes)
        
        # Convert naive datetime to timezone-aware datetime using the business timezone
        tz_str = os.getenv("CALCOM_TIMEZONE", "America/Toronto")
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(tz_str)
        except Exception:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo("America/Toronto")
            
        start_aware = start_dt.replace(tzinfo=tz)
        end_aware = end_dt.replace(tzinfo=tz)
        
        start_iso = start_aware.isoformat()
        end_iso = end_aware.isoformat()
        
        calendar_id = os.getenv("GOOGLE_CALENDAR_ID", "primary")
        
        # Build the Calendar API service (synchronous call inside an async func, 
        # but FreeBusy query is usually fast enough not to block the event loop for long)
        service = build('calendar', 'v3', credentials=creds, cache_discovery=False)
        
        body = {
            "timeMin": start_iso,
            "timeMax": end_iso,
            "items": [{"id": calendar_id}]
        }
        
        # Query FreeBusy API
        eventsResult = service.freebusy().query(body=body).execute()
        calendars = eventsResult.get('calendars', {})
        calendar_info = calendars.get(calendar_id, {})
        busy_slots = calendar_info.get('busy', [])
        
        if busy_slots:
            logger.info("Google Calendar slot %s %s is BUSY.", date_str, time_str)
            return False
            
        logger.info("Google Calendar slot %s %s is FREE.", date_str, time_str)
        return True
        
    except Exception as exc:
        logger.error("check_google_calendar_availability failed: %s", exc)
        # Default to True on error so we don't block bookings entirely
        return True

async def insert_google_calendar_event(name: str, phone: str, date_str: str, time_str: str, service: str, insurance: str = "", duration_minutes: int = 30) -> str:
    """
    Creates an event on Google Calendar.
    Returns the event ID if successful, or raises an Exception.
    """
    creds = _get_credentials()
    if not creds or not creds.valid:
        logger.warning("No valid token.json found. Skipping Google Calendar insertion.")
        return ""
        
    start_dt = datetime.datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
    end_dt = start_dt + datetime.timedelta(minutes=duration_minutes)
    
    tz_str = os.getenv("CALCOM_TIMEZONE", "America/Toronto")
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(tz_str)
    except Exception:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("America/Toronto")
        
    start_aware = start_dt.replace(tzinfo=tz)
    end_aware = end_dt.replace(tzinfo=tz)
    
    calendar_id = os.getenv("GOOGLE_CALENDAR_ID", "primary")
    service_api = build('calendar', 'v3', credentials=creds, cache_discovery=False)
    
    event_body = {
        'summary': f'{service} with {name}',
        'description': f'Phone: {phone}\nService: {service}\nInsurance: {insurance}\nBooked via OutboundAI',
        'start': {
            'dateTime': start_aware.isoformat(),
            'timeZone': tz_str,
        },
        'end': {
            'dateTime': end_aware.isoformat(),
            'timeZone': tz_str,
        },
    }
    
    event = service_api.events().insert(calendarId=calendar_id, body=event_body).execute()
    logger.info("Google Calendar event created: %s", event.get('htmlLink'))
    return event.get('id', '')
