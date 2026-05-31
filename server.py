"""
server.py — OutboundAI FastAPI backend
All REST endpoints + APScheduler campaign runner
"""

import asyncio
import json
import logging
import os
import uuid
from datetime import datetime, timedelta
from typing import Optional, List

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import db
from db import (
    init_db, get_all_settings, save_settings, get_setting, set_setting,
    get_errors, get_logs, clear_errors, log_error,
    insert_appointment, get_all_appointments, cancel_appointment,
    get_all_calls, update_call_notes, get_contacts,
    get_stats,
    create_campaign, get_all_campaigns, get_campaign,
    update_campaign_status, update_campaign_run_stats, delete_campaign,
    get_contact_memory, add_contact_memory,
    get_all_agent_profiles, get_agent_profile, create_agent_profile,
    update_agent_profile, delete_agent_profile, set_default_agent_profile,
)

load_dotenv(".env")
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("outbound-server")

app = FastAPI(title="OutboundAI", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve UI
try:
    app.mount("/ui", StaticFiles(directory="ui", html=True), name="ui")
except Exception:
    pass

scheduler = AsyncIOScheduler()


# ═══════════════════════════════════════════════════════════════════
# Pydantic models
# ═══════════════════════════════════════════════════════════════════

class SingleCallRequest(BaseModel):
    phone_number: str
    lead_name: Optional[str] = "there"
    system_prompt: Optional[str] = None
    agent_profile_id: Optional[str] = None


class BatchCallRequest(BaseModel):
    contacts: List[dict]
    delay_seconds: int = 3
    system_prompt: Optional[str] = None
    agent_profile_id: Optional[str] = None


class CampaignCreate(BaseModel):
    name: str
    contacts: List[dict]
    schedule_type: str = "once"          # once | daily | weekdays
    schedule_time: str = "09:00"         # HH:MM
    call_delay_seconds: int = 3
    system_prompt: Optional[str] = None
    agent_profile_id: Optional[str] = None


class AppointmentCreate(BaseModel):
    name: str
    phone: str
    date: str
    time: str
    service: str


class SettingsUpdate(BaseModel):
    settings: dict


class NotesUpdate(BaseModel):
    notes: str


class AgentProfileCreate(BaseModel):
    name: str
    voice: str = "Aoede"
    model: str = "gemini-2.5-flash-native-audio-latest"
    system_prompt: Optional[str] = None
    enabled_tools: str = "[]"
    is_default: bool = False


class AgentProfileUpdate(BaseModel):
    name: Optional[str] = None
    voice: Optional[str] = None
    model: Optional[str] = None
    system_prompt: Optional[str] = None
    enabled_tools: Optional[str] = None
    is_default: Optional[bool] = None


# ═══════════════════════════════════════════════════════════════════
# Lifecycle
# ═══════════════════════════════════════════════════════════════════

@app.on_event("startup")
async def startup():
    init_db()
    scheduler.start()
    await _reschedule_all_campaigns()
    logger.info("✅ OutboundAI server started")


@app.on_event("shutdown")
async def shutdown():
    scheduler.shutdown(wait=False)


# ═══════════════════════════════════════════════════════════════════
# Health & root
# ═══════════════════════════════════════════════════════════════════

@app.get("/")
async def root():
    return FileResponse("ui/index.html") if os.path.exists("ui/index.html") else {"status": "ok", "service": "OutboundAI"}


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "timestamp": datetime.now().isoformat(),
        "livekit_url": os.getenv("LIVEKIT_URL", "not set"),
        "supabase": bool(os.getenv("SUPABASE_URL")),
    }


# ═══════════════════════════════════════════════════════════════════
# Calling — dispatch via LiveKit SIP
# ═══════════════════════════════════════════════════════════════════

async def _dispatch_call(
    phone_number: str,
    lead_name: str = "there",
    system_prompt: Optional[str] = None,
    agent_profile_id: Optional[str] = None,
) -> dict:
    """Create a LiveKit SIP outbound call room and dispatch agent."""
    lk_url = os.getenv("LIVEKIT_URL", "")
    lk_key = os.getenv("LIVEKIT_API_KEY", "")
    lk_secret = os.getenv("LIVEKIT_API_SECRET", "")
    trunk_id = os.getenv("OUTBOUND_TRUNK_ID", "")

    if not all([lk_url, lk_key, lk_secret]):
        raise ValueError("LiveKit credentials not configured")
    if not trunk_id:
        raise ValueError("OUTBOUND_TRUNK_ID not set")

    import livekit.api as lkapi

    lk = lkapi.LiveKitAPI(url=lk_url, api_key=lk_key, api_secret=lk_secret)
    room_name = f"call-{uuid.uuid4().hex[:12]}"

    # Build participant metadata passed to the agent
    metadata = json.dumps({
        "phone_number": phone_number,
        "lead_name": lead_name,
        "agent_profile_id": agent_profile_id or "",
        "system_prompt": system_prompt or "",
    })

    try:
        # Create dispatch (agent will auto-join)
        dispatch = await lk.agent_dispatch.create_dispatch(
            lkapi.CreateAgentDispatchRequest(
                agent_name="outbound-ai",
                room=room_name,
                metadata=metadata,
            )
        )

        # Initiate SIP outbound call
        sip_call = await lk.sip.create_sip_participant(
            lkapi.CreateSIPParticipantRequest(
                sip_trunk_id=trunk_id,
                sip_call_to=phone_number,
                room_name=room_name,
                participant_identity=f"sip_{phone_number}",
                participant_name=lead_name,
                participant_metadata=metadata,
                play_ringtone=False,
            )
        )

        logger.info("Dispatched call to %s in room %s", phone_number, room_name)
        return {
            "room_name": room_name,
            "dispatch_id": dispatch.dispatch_id if hasattr(dispatch, "dispatch_id") else str(dispatch),
            "sip_participant_id": sip_call.participant_id if hasattr(sip_call, "participant_id") else str(sip_call),
            "phone_number": phone_number,
        }
    finally:
        await lk.aclose()


@app.post("/call/single")
async def call_single(req: SingleCallRequest):
    """Trigger a single outbound AI call."""
    try:
        result = await _dispatch_call(
            phone_number=req.phone_number,
            lead_name=req.lead_name or "there",
            system_prompt=req.system_prompt,
            agent_profile_id=req.agent_profile_id,
        )
        await log_error("server", f"Call dispatched to {req.phone_number}", "", "info")
        return {"success": True, **result}
    except Exception as exc:
        await log_error("server", f"Call dispatch failed: {exc}", str(exc), "error")
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/call/batch")
async def call_batch(req: BatchCallRequest):
    """Dispatch multiple calls sequentially with a delay between each."""
    results = []
    for contact in req.contacts:
        phone = contact.get("phone") or contact.get("phone_number", "")
        name = contact.get("name") or contact.get("lead_name", "there")
        if not phone:
            results.append({"phone": phone, "status": "skipped", "reason": "no phone"})
            continue
        try:
            r = await _dispatch_call(
                phone_number=phone,
                lead_name=name,
                system_prompt=req.system_prompt,
                agent_profile_id=req.agent_profile_id,
            )
            results.append({"phone": phone, "status": "dispatched", **r})
        except Exception as exc:
            results.append({"phone": phone, "status": "failed", "error": str(exc)})
        if req.delay_seconds > 0:
            await asyncio.sleep(req.delay_seconds)
    return {"results": results, "total": len(results)}


@app.post("/call/csv")
async def call_csv(
    file: UploadFile = File(...),
    delay_seconds: int = Form(3),
    system_prompt: str = Form(""),
    agent_profile_id: str = Form(""),
):
    """Upload a CSV of contacts and dispatch calls."""
    import csv, io
    content = await file.read()
    text = content.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    contacts = []
    for row in reader:
        phone = row.get("phone") or row.get("phone_number") or row.get("Phone") or ""
        name = row.get("name") or row.get("Name") or row.get("lead_name") or "there"
        if phone:
            contacts.append({"phone": phone.strip(), "name": name.strip()})

    req = BatchCallRequest(
        contacts=contacts,
        delay_seconds=delay_seconds,
        system_prompt=system_prompt or None,
        agent_profile_id=agent_profile_id or None,
    )
    return await call_batch(req)


# ═══════════════════════════════════════════════════════════════════
# Campaigns
# ═══════════════════════════════════════════════════════════════════

async def _run_campaign(campaign_id: str) -> None:
    """Execute a campaign: dispatch all contacts and update stats."""
    camp = await get_campaign(campaign_id)
    if not camp or camp.get("status") == "paused":
        return

    contacts = json.loads(camp.get("contacts_json") or "[]")
    delay = int(camp.get("call_delay_seconds") or 3)
    system_prompt = camp.get("system_prompt")
    agent_profile_id = camp.get("agent_profile_id")

    dispatched = 0
    failed = 0
    for contact in contacts:
        phone = contact.get("phone") or contact.get("phone_number", "")
        name = contact.get("name") or contact.get("lead_name", "there")
        if not phone:
            failed += 1
            continue
        try:
            await _dispatch_call(
                phone_number=phone,
                lead_name=name,
                system_prompt=system_prompt,
                agent_profile_id=agent_profile_id,
            )
            dispatched += 1
        except Exception as exc:
            logger.error("Campaign %s: call to %s failed: %s", campaign_id, phone, exc)
            failed += 1
        if delay > 0:
            await asyncio.sleep(delay)

    await update_campaign_run_stats(campaign_id, dispatched, failed)
    logger.info("Campaign %s complete: %d dispatched, %d failed", campaign_id, dispatched, failed)


async def _reschedule_all_campaigns() -> None:
    """On startup, re-register all active campaign schedules."""
    try:
        campaigns = await get_all_campaigns()
        for camp in campaigns:
            if camp.get("status") not in ("active", "running"):
                continue
            _schedule_campaign(camp)
    except Exception as exc:
        logger.warning("Failed to reschedule campaigns: %s", exc)


def _schedule_campaign(camp: dict) -> None:
    """Register an APScheduler job for a campaign."""
    campaign_id = camp["id"]
    schedule_type = camp.get("schedule_type", "once")
    schedule_time = camp.get("schedule_time", "09:00")
    job_id = f"campaign_{campaign_id}"

    # Remove existing job if any
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)

    try:
        hour, minute = map(int, schedule_time.split(":"))
    except ValueError:
        hour, minute = 9, 0

    if schedule_type == "once":
        run_time = datetime.now() + timedelta(seconds=5)
        scheduler.add_job(
            _run_campaign, trigger=DateTrigger(run_date=run_time),
            args=[campaign_id], id=job_id, replace_existing=True,
        )
    elif schedule_type == "daily":
        scheduler.add_job(
            _run_campaign, trigger=CronTrigger(hour=hour, minute=minute),
            args=[campaign_id], id=job_id, replace_existing=True,
        )
    elif schedule_type == "weekdays":
        scheduler.add_job(
            _run_campaign,
            trigger=CronTrigger(day_of_week="mon-fri", hour=hour, minute=minute),
            args=[campaign_id], id=job_id, replace_existing=True,
        )
    logger.info("Scheduled campaign %s (%s at %s)", campaign_id, schedule_type, schedule_time)


@app.get("/campaigns")
async def list_campaigns():
    return await get_all_campaigns()


@app.post("/campaigns")
async def create_campaign_endpoint(req: CampaignCreate):
    contacts_json = json.dumps(req.contacts)
    campaign_id = await create_campaign(
        name=req.name,
        contacts_json=contacts_json,
        schedule_type=req.schedule_type,
        schedule_time=req.schedule_time,
        call_delay_seconds=req.call_delay_seconds,
        system_prompt=req.system_prompt,
        agent_profile_id=req.agent_profile_id,
    )
    camp = await get_campaign(campaign_id)
    _schedule_campaign(camp)
    return {"success": True, "campaign_id": campaign_id}


@app.get("/campaigns/{campaign_id}")
async def get_campaign_endpoint(campaign_id: str):
    camp = await get_campaign(campaign_id)
    if not camp:
        raise HTTPException(status_code=404, detail="Campaign not found")
    return camp


@app.post("/campaigns/{campaign_id}/pause")
async def pause_campaign(campaign_id: str):
    ok = await update_campaign_status(campaign_id, "paused")
    job_id = f"campaign_{campaign_id}"
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
    return {"success": ok}


@app.post("/campaigns/{campaign_id}/resume")
async def resume_campaign(campaign_id: str):
    ok = await update_campaign_status(campaign_id, "active")
    camp = await get_campaign(campaign_id)
    if camp:
        _schedule_campaign(camp)
    return {"success": ok}


@app.post("/campaigns/{campaign_id}/run")
async def run_campaign_now(campaign_id: str):
    """Immediately trigger a campaign run."""
    asyncio.create_task(_run_campaign(campaign_id))
    return {"success": True, "message": "Campaign triggered"}


@app.delete("/campaigns/{campaign_id}")
async def delete_campaign_endpoint(campaign_id: str):
    job_id = f"campaign_{campaign_id}"
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
    ok = await delete_campaign(campaign_id)
    return {"success": ok}


# ═══════════════════════════════════════════════════════════════════
# Appointments
# ═══════════════════════════════════════════════════════════════════

@app.get("/appointments")
async def list_appointments(date: Optional[str] = Query(None)):
    return await get_all_appointments(date_filter=date)


@app.post("/appointments")
async def create_appointment(req: AppointmentCreate):
    booking_id = await insert_appointment(req.name, req.phone, req.date, req.time, req.service)
    return {"success": True, "booking_id": booking_id}


@app.delete("/appointments/{appointment_id}")
async def cancel_appointment_endpoint(appointment_id: str):
    ok = await cancel_appointment(appointment_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Appointment not found or already cancelled")
    return {"success": True}


# ═══════════════════════════════════════════════════════════════════
# Call Logs
# ═══════════════════════════════════════════════════════════════════

@app.get("/calls")
async def list_calls(page: int = Query(1, ge=1), limit: int = Query(20, le=100)):
    return await get_all_calls(page=page, limit=limit)


@app.patch("/calls/{call_id}/notes")
async def update_notes(call_id: str, req: NotesUpdate):
    ok = await update_call_notes(call_id, req.notes)
    return {"success": ok}


# ═══════════════════════════════════════════════════════════════════
# CRM / Contacts
# ═══════════════════════════════════════════════════════════════════

@app.get("/contacts")
async def list_contacts():
    return await get_contacts()


@app.get("/contacts/{phone}/memory")
async def get_memory(phone: str):
    return await get_contact_memory(phone)


@app.post("/contacts/{phone}/memory")
async def add_memory(phone: str, body: dict):
    insight = body.get("insight", "")
    if not insight:
        raise HTTPException(status_code=400, detail="insight required")
    await add_contact_memory(phone, insight)
    return {"success": True}


# ═══════════════════════════════════════════════════════════════════
# Stats / Analytics
# ═══════════════════════════════════════════════════════════════════

@app.get("/stats")
async def get_stats_endpoint():
    return await get_stats()


# ═══════════════════════════════════════════════════════════════════
# Settings (BYOK)
# ═══════════════════════════════════════════════════════════════════

@app.get("/settings")
async def get_settings():
    return await get_all_settings()


@app.post("/settings")
async def update_settings(req: SettingsUpdate):
    await save_settings(req.settings)
    # Reload env
    for k, v in req.settings.items():
        if v:
            os.environ[k] = str(v)
    return {"success": True}


@app.get("/settings/{key}")
async def get_one_setting(key: str):
    value = await get_setting(key)
    return {"key": key, "value": value}


@app.post("/settings/{key}")
async def set_one_setting(key: str, body: dict):
    value = body.get("value", "")
    await set_setting(key, value)
    if value:
        os.environ[key] = value
    return {"success": True}


# ═══════════════════════════════════════════════════════════════════
# Logs / Error Monitoring
# ═══════════════════════════════════════════════════════════════════

@app.get("/logs")
async def list_logs(
    level: Optional[str] = Query(None),
    source: Optional[str] = Query(None),
    limit: int = Query(200, le=500),
):
    return await get_logs(level=level, source=source, limit=limit)


@app.delete("/logs")
async def clear_logs():
    await clear_errors()
    return {"success": True}


# ═══════════════════════════════════════════════════════════════════
# Agent Profiles
# ═══════════════════════════════════════════════════════════════════

@app.get("/agent-profiles")
async def list_agent_profiles():
    return await get_all_agent_profiles()


@app.post("/agent-profiles")
async def create_profile(req: AgentProfileCreate):
    profile_id = await create_agent_profile(
        name=req.name,
        voice=req.voice,
        model=req.model,
        system_prompt=req.system_prompt,
        enabled_tools=req.enabled_tools,
        is_default=req.is_default,
    )
    return {"success": True, "profile_id": profile_id}


@app.get("/agent-profiles/{profile_id}")
async def get_profile(profile_id: str):
    profile = await get_agent_profile(profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")
    return profile


@app.patch("/agent-profiles/{profile_id}")
async def update_profile(profile_id: str, req: AgentProfileUpdate):
    updates = {k: v for k, v in req.dict().items() if v is not None}
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")
    ok = await update_agent_profile(profile_id, updates)
    return {"success": ok}


@app.delete("/agent-profiles/{profile_id}")
async def delete_profile(profile_id: str):
    ok = await delete_agent_profile(profile_id)
    return {"success": ok}


@app.post("/agent-profiles/{profile_id}/set-default")
async def set_default_profile(profile_id: str):
    await set_default_agent_profile(profile_id)
    return {"success": True}


# ═══════════════════════════════════════════════════════════════════
# Scheduler status
# ═══════════════════════════════════════════════════════════════════

@app.get("/scheduler/jobs")
async def list_scheduled_jobs():
    jobs = []
    for job in scheduler.get_jobs():
        jobs.append({
            "id": job.id,
            "name": job.name,
            "next_run": job.next_run_time.isoformat() if job.next_run_time else None,
        })
    return {"jobs": jobs}
