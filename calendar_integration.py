import os
import datetime
import logging
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

logger = logging.getLogger("calendar-integration")
SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]

def _get_credentials():
    token_path = os.path.join(os.path.dirname(__file__), "token.json")
    if os.path.exists(token_path):
        return Credentials.from_authorized_user_file(token_path, SCOPES)
    
    # Fallback for Coolify deployment: load from database setting injected into env
    token_json_str = os.getenv("GOOGLE_OAUTH_TOKEN", "")
    if token_json_str:
        try:
            import json
            info = json.loads(token_json_str)
            return Credentials.from_authorized_user_info(info, SCOPES)
        except Exception as exc:
            logger.error("Failed to parse GOOGLE_OAUTH_TOKEN: %s", exc)
            
    return None

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
        
        # Convert naive datetime to local timezone-aware datetime for accurate API queries
        start_aware = start_dt.astimezone()
        end_aware = end_dt.astimezone()
        
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
