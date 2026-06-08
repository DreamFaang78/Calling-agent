import asyncio
import logging
import os
import time
from typing import Optional

from livekit import agents, api
from livekit.agents import llm

from db import (
    check_slot, get_next_available, insert_appointment, log_call, log_error,
    get_calls_by_phone, get_appointments_by_phone,
    add_contact_memory, get_contact_memory, compress_contact_memory,
    insert_lead,
)

logger = logging.getLogger("appointment-tools")


async def _log(msg: str, detail: str = "", level: str = "info") -> None:
    try:
        await log_error("agent", msg, detail, level)
    except Exception:
        pass


class AppointmentTools(llm.ToolContext):
    """All function tools available to the appointment-booking agent."""

    def __init__(
        self,
        ctx: agents.JobContext,
        phone_number: Optional[str] = None,
        lead_name: Optional[str] = None,
        call_direction: str = "outbound",
        lead_source: str = "",
    ):
        self.ctx = ctx
        self.phone_number = phone_number
        self.lead_name = lead_name
        self.call_direction = call_direction
        self.lead_source = lead_source or ("google_my_business" if call_direction == "inbound" else "outbound_campaign")
        self._call_start_time = time.time()
        self._sip_domain = os.getenv("VOBIZ_SIP_DOMAIN", "")
        self.recording_url: Optional[str] = None
        super().__init__(tools=[])

    def build_tool_list(self, enabled: list) -> list:
        """Return tool methods filtered by the enabled list. Empty list = all enabled."""
        all_methods = [
            self.check_availability, self.book_appointment, self.end_call,
            self.transfer_to_human, self.send_sms_confirmation, self.lookup_contact,
            self.remember_details, self.book_calcom, self.cancel_calcom,
            self.capture_lead,
        ]
        if not enabled:
            return all_methods
        name_map = {m.__name__: m for m in all_methods}
        return [name_map[n] for n in enabled if n in name_map]

    @llm.function_tool
    async def check_availability(self, date: str, time: str) -> str:
        """
        Check whether a date/time slot is available for booking.
        Call this BEFORE attempting to book whenever the lead proposes a date/time.
        date format: YYYY-MM-DD  |  time format: HH:MM (24-hour)
        Returns 'available' or 'unavailable: next available slot is <slot>'.
        """
        try:
            from db import is_within_business_hours
            if not await is_within_business_hours(date, time):
                next_slot = await get_next_available(date, time)
                return f"unavailable: that time is outside our business hours. Next available is {next_slot}"

            # 1. Check Google Calendar API first (if configured)
            from calendar_integration import check_google_calendar_availability
            is_free_on_google = await check_google_calendar_availability(date, time)
            if not is_free_on_google:
                next_slot = await get_next_available(date, time)
                return f"unavailable: slot is busy on Google Calendar. Next available is {next_slot}"

            # 2. Check local database
            if await check_slot(date, time):
                return "available"
                
            next_slot = await get_next_available(date, time)
            return f"unavailable: next available slot is {next_slot}"
        except Exception as exc:
            logger.error("check_availability error: %s", exc)
            # Fail OPEN: never make the caller hear a system problem. Treat the
            # slot as bookable and let book_appointment + the human advisor
            # resolve any rare conflict. Booking beats sounding broken.
            return "available"

    @llm.function_tool
    async def book_appointment(self, name: str, phone: str, date: str, time: str, service: str, insurance: str) -> str:
        """
        Book an appointment.
        Call ONLY after the lead confirms date, time, service, and THEIR INSURANCE TYPE.
        name: lead's full name | phone: with country code | date: YYYY-MM-DD | time: HH:MM | service: type | insurance: type of insurance (e.g. 'Medicare', 'BlueCross', 'None')
        """
        try:
            booking_id = await insert_appointment(name, phone, date, time, service, insurance)
            
            # Also try to book in Google Calendar
            try:
                from calendar_integration import insert_google_calendar_event
                await insert_google_calendar_event(name, phone, date, time, service, insurance)
            except Exception as cal_exc:
                logger.error("Failed to insert into Google Calendar: %s", cal_exc)
                
            return f"Confirmed! Booking ID: {booking_id}. See you on {date} at {time} for {service}."
        except Exception as exc:
            logger.error("book_appointment error: %s", exc)
            return "Technical issue saving the booking. Our team will confirm shortly."

    @llm.function_tool
    async def end_call(self, outcome: str, reason: str = "") -> str:
        """
        End the call and log the outcome. ALWAYS call this before the call ends.
        outcome: 'booked' | 'not_interested' | 'wrong_number' | 'voicemail' | 'no_answer' | 'callback_requested'
        reason: brief description
        """
        duration = int(time.time() - self._call_start_time)
        try:
            await log_call(
                phone_number=self.phone_number or "unknown",
                lead_name=self.lead_name,
                outcome=outcome,
                reason=reason,
                duration_seconds=duration,
                recording_url=self.recording_url,
            )
        except Exception as exc:
            logger.error("Failed to log call: %s", exc)
        try:
            await self.ctx.room.disconnect()
        except Exception:
            pass
        return "Call ended."

    @llm.function_tool
    async def transfer_to_human(self, reason: str) -> str:
        """
        Transfer the call to a human agent via SIP REFER.
        Call when lead requests a human, is angry, or has a complex issue.
        reason: why you're transferring
        """
        destination = os.getenv("DEFAULT_TRANSFER_NUMBER", "")
        if not destination:
            return "Transfer unavailable: no fallback number configured."
        if "@" not in destination:
            clean = destination.replace("tel:", "").replace("sip:", "")
            destination = (
                f"sip:{clean}@{self._sip_domain}"
                if self._sip_domain
                else f"tel:{clean}"
            )
        elif not destination.startswith("sip:"):
            destination = f"sip:{destination}"

        participant_identity = f"sip_{self.phone_number}" if self.phone_number else None
        if not participant_identity:
            for p in self.ctx.room.remote_participants.values():
                participant_identity = p.identity
                break
        if not participant_identity:
            return "Transfer failed: could not identify caller."
        try:
            await self.ctx.api.sip.transfer_sip_participant(
                api.TransferSIPParticipantRequest(
                    room_name=self.ctx.room.name,
                    participant_identity=participant_identity,
                    transfer_to=destination,
                    play_dialtone=False,
                )
            )
            return "Transferring you to a human agent now. Please hold."
        except Exception as exc:
            logger.error("transfer_to_human error: %s", exc)
            return "Transfer failed. Please call us back directly."

    @llm.function_tool
    async def send_sms_confirmation(self, phone: str, message: str) -> str:
        """
        Send SMS confirmation after a successful booking. Skips silently if Twilio not configured.
        phone: lead's phone | message: text to send
        """
        sid = os.getenv("TWILIO_ACCOUNT_SID", "")
        token = os.getenv("TWILIO_AUTH_TOKEN", "")
        from_num = os.getenv("TWILIO_FROM_NUMBER", "")
        if not (sid and token and from_num):
            return "SMS skipped: Twilio not configured."
        try:
            from twilio.rest import Client
            loop = asyncio.get_event_loop()
            client = Client(sid, token)
            await loop.run_in_executor(
                None, lambda: client.messages.create(body=message, from_=from_num, to=phone)
            )
            return f"SMS sent to {phone}."
        except Exception as exc:
            logger.error("send_sms_confirmation error: %s", exc)
            return "SMS delivery failed, but booking is confirmed."

    @llm.function_tool
    async def lookup_contact(self, phone: str) -> str:
        """
        Look up a contact's full history. Call at the START of every call before engaging.
        phone: the lead's phone number with country code
        Returns call history, appointments, and remembered details.
        """
        try:
            calls = await get_calls_by_phone(phone)
            appointments = await get_appointments_by_phone(phone)
            memories = await get_contact_memory(phone)
            if not calls and not appointments and not memories:
                return f"No history for {phone}. First-time contact."
            lines = [f"Contact history for {phone}:"]
            if memories:
                lines.append(f"\nREMEMBERED ({len(memories)} notes):")
                for m in memories[:10]:
                    lines.append(f"  • {m['insight']}")
            if calls:
                lines.append(f"\nCALL HISTORY ({len(calls)} calls):")
                for c in calls[:5]:
                    ts = (c.get("timestamp") or "")[:16]
                    lines.append(f"  • {ts} — {c.get('outcome', '?')}: {c.get('reason', '')}")
            if appointments:
                lines.append(f"\nAPPOINTMENTS ({len(appointments)}):")
                for a in appointments[:3]:
                    lines.append(
                        f"  • {a.get('date')} {a.get('time')} — {a.get('service')} [{a.get('status')}]"
                    )
            return "\n".join(lines)
        except Exception as exc:
            logger.error("lookup_contact error: %s", exc)
            return "Unable to retrieve contact history."

    @llm.function_tool
    async def remember_details(self, insight: str) -> str:
        """
        Store a key insight about this lead for future calls.
        Use whenever you learn something useful: preferences, objections, timing, family info.
        Examples: "Prefers morning calls", "Has 2 kids, interested in family plan", "Callback in 2 weeks"
        insight: the detail to remember
        """
        if not self.phone_number:
            return "Cannot remember — no phone number for this call."
        try:
            await add_contact_memory(self.phone_number, insight)
            memories = await get_contact_memory(self.phone_number)
            if len(memories) >= 5:
                asyncio.create_task(self._compress_memories())
            return f"Remembered: {insight}"
        except Exception as exc:
            logger.error("remember_details error: %s", exc)
            return "Could not save detail."

    async def _compress_memories(self) -> None:
        """Use Gemini Flash to compress multiple memories into concise bullets.

        Uses the bundled google-genai SDK (the async client). Do NOT use the
        deprecated google-generativeai package — it is not installed and
        conflicts with google-genai (see AGENTS.md).
        """
        try:
            memories = await get_contact_memory(self.phone_number)
            if len(memories) < 5:
                return
            api_key = os.getenv("GOOGLE_API_KEY", "")
            if not api_key:
                return
            from google import genai
            client = genai.Client(api_key=api_key)
            # Use a text model here, NOT the realtime native-audio model in GEMINI_MODEL.
            # gemini-2.0-flash is retired for new accounts; default to 2.5-flash.
            model = os.getenv("MEMORY_COMPRESSION_MODEL", "gemini-2.5-flash")
            bullet_list = "\n".join(f"- {m['insight']}" for m in memories)
            prompt = (
                "Compress these notes about a sales contact into 3-5 concise bullets. "
                f"Keep all key facts.\n\n{bullet_list}"
            )
            response = await client.aio.models.generate_content(
                model=model, contents=prompt
            )
            text = (response.text or "").strip()
            if text:
                await compress_contact_memory(self.phone_number, text)
        except Exception as exc:
            logger.warning("Memory compression failed: %s", exc)

    @llm.function_tool
    async def capture_lead(
        self,
        name: str,
        requirement: str,
        urgency: str = "",
        budget: str = "",
        timeline: str = "",
        notes: str = "",
    ) -> str:
        """
        Save what an inbound caller is looking for — use this once you understand
        their need, REGARDLESS of whether they book an appointment on this call.
        This is how the business finds out about the lead and follows up.
        Call it once per call, after the caller has shared what they need (don't
        interrogate — just save what naturally comes up in conversation; leave a
        field blank if it was never mentioned).

        name: caller's name | requirement: what they're looking for/need in their words
        urgency: how soon they want it (e.g. 'this week', 'just researching', 'ASAP')
        budget: budget range if they mentioned one (e.g. '$5k-10k', 'not discussed')
        timeline: when they'd want to start/move forward
        notes: anything else worth relaying to the team (location, prior provider, etc.)
        """
        try:
            lead_id = await insert_lead(
                phone=self.phone_number or "unknown",
                name=name or self.lead_name,
                requirement=requirement,
                urgency=urgency,
                budget=budget,
                timeline=timeline,
                notes=notes,
                source=self.lead_source,
                status="new",
            )
            asyncio.create_task(self._alert_owner_of_lead(name, requirement, urgency, budget))
            return "Got it, noted down for our team."
        except Exception as exc:
            logger.error("capture_lead error: %s", exc)
            await _log("capture_lead failed", str(exc), "error")
            return "Noted — though I had trouble saving it, I'll make sure the team still hears about this."

    async def _alert_owner_of_lead(self, name: str, requirement: str, urgency: str, budget: str) -> None:
        """Text the business owner a one-line summary the moment a lead is captured.

        Reuses the Twilio config already required for send_sms_confirmation —
        no new integration, just a different recipient (LEAD_ALERT_PHONE_NUMBER).
        """
        owner_phone = os.getenv("LEAD_ALERT_PHONE_NUMBER", "")
        sid = os.getenv("TWILIO_ACCOUNT_SID", "")
        token = os.getenv("TWILIO_AUTH_TOKEN", "")
        from_num = os.getenv("TWILIO_FROM_NUMBER", "")
        if not (owner_phone and sid and token and from_num):
            return
        try:
            who = name or self.lead_name or "Unknown caller"
            parts = [f"New lead: {who}"]
            if requirement:
                parts.append(f"wants {requirement}")
            if urgency:
                parts.append(f"urgency: {urgency}")
            if budget:
                parts.append(f"budget: {budget}")
            parts.append(f"Call back: {self.phone_number or 'unknown'}")
            message = ". ".join(parts)
            from twilio.rest import Client
            loop = asyncio.get_event_loop()
            client = Client(sid, token)
            await loop.run_in_executor(
                None, lambda: client.messages.create(body=message[:1500], from_=from_num, to=owner_phone)
            )
        except Exception as exc:
            logger.warning("Lead alert SMS failed: %s", exc)

    @llm.function_tool
    async def book_calcom(
        self, name: str, email: str, date: str, start_time: str, notes: str = ""
    ) -> str:
        """
        Book in Cal.com calendar after book_appointment succeeds.
        name: full name | email: lead's email | date: YYYY-MM-DD | start_time: HH:MM | notes: optional
        """
        api_key = os.getenv("CALCOM_API_KEY", "")
        event_type_id = os.getenv("CALCOM_EVENT_TYPE_ID", "")
        timezone = os.getenv("CALCOM_TIMEZONE", "Asia/Kolkata")
        if not api_key or not event_type_id:
            return "Cal.com not configured — skipping. Add CALCOM_API_KEY and CALCOM_EVENT_TYPE_ID."
        try:
            from datetime import datetime as _dt
            start_dt = _dt.strptime(f"{date} {start_time}", "%Y-%m-%d %H:%M")
            start_iso = start_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")
            import httpx
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    "https://api.cal.com/v1/bookings",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "eventTypeId": int(event_type_id),
                        "start": start_iso,
                        "timeZone": timezone,
                        "responses": {"name": name, "email": email, "notes": notes},
                        "metadata": {"source": "OutboundAI"},
                        "language": "en",
                    },
                )
            data = resp.json()
            if resp.status_code not in (200, 201):
                raise ValueError(data.get("message") or str(data))
            uid = data.get("uid", "")
            return f"Cal.com booked. UID: {uid}"
        except Exception as exc:
            logger.error("book_calcom error: %s", exc)
            return f"Cal.com booking failed: {exc}"

    @llm.function_tool
    async def cancel_calcom(self, booking_uid: str, reason: str = "") -> str:
        """
        Cancel a Cal.com booking by UID.
        booking_uid: from book_calcom | reason: optional
        """
        api_key = os.getenv("CALCOM_API_KEY", "")
        if not api_key:
            return "Cal.com not configured."
        try:
            import httpx
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.delete(
                    f"https://api.cal.com/v1/bookings/{booking_uid}",
                    headers={"Authorization": f"Bearer {api_key}"},
                    params={"reason": reason} if reason else {},
                )
            if resp.status_code not in (200, 204):
                raise ValueError(f"HTTP {resp.status_code}")
            return f"Cancelled Cal.com booking {booking_uid}."
        except Exception as exc:
            logger.error("cancel_calcom error: %s", exc)
            return f"Cancellation failed: {exc}"
