import asyncio
import json
import logging
import os
import ssl
import certifi
from typing import Optional

from dotenv import load_dotenv

# ── Patch SSL to use certifi CA bundle before any network imports ─────────────
_orig_ssl = ssl.create_default_context


def _certifi_ssl(purpose=ssl.Purpose.SERVER_AUTH, **kwargs):
    if not kwargs.get("cafile") and not kwargs.get("capath") and not kwargs.get("cadata"):
        kwargs["cafile"] = certifi.where()
    return _orig_ssl(purpose, **kwargs)


ssl.create_default_context = _certifi_ssl

from livekit import agents, api, rtc
from livekit.agents import Agent, AgentSession, RoomInputOptions

try:
    from livekit.agents import RoomOptions as _RoomOptions
    _HAS_ROOM_OPTIONS = True
except ImportError:
    _HAS_ROOM_OPTIONS = False

from livekit.plugins import noise_cancellation, silero

from db import init_db, log_error, get_enabled_tools, get_agent_profile
from prompts import build_prompt
from tools import AppointmentTools

load_dotenv(".env")
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("outbound-agent")

SIP_DOMAIN = os.getenv("VOBIZ_SIP_DOMAIN", "")


class GreetingAgent(Agent):
    """Agent that speaks first the moment it becomes active.

    The opening line is triggered from ``on_enter`` rather than right after
    ``session.start()``. ``on_enter`` is invoked by the framework only once the
    AgentSession is fully running and this agent is the active one, which avoids
    the "AgentSession isn't running" race that happens when generate_reply() is
    called too early.
    """

    async def on_enter(self) -> None:
        # Log to the DB-backed error_logs table so this shows in the dashboard
        # Live Logs (logger.* only goes to container stdout, which the dashboard
        # does not display). This is how we see whether the greeting actually
        # fires on a real call.
        await _log("info", "on_enter: agent active, requesting opening greeting")
        try:
            # We must pass an explicit instruction here; otherwise, Gemini Realtime 
            # will wait for the user to speak first.
            handle = self.session.generate_reply(
                instructions="Please say hello and greet the user warmly."
            )
            # Await the speech handle if the framework returns an awaitable one,
            # so we surface any error raised while producing the greeting.
            if hasattr(handle, "__await__"):
                await handle
            await _log("info", "on_enter: generate_reply dispatched OK")
        except Exception as exc:
            # Surface the real failure to the dashboard instead of silently
            # leaving the call dead-air.
            await _log("error", f"on_enter greeting failed: {exc}", repr(exc))


async def _log(level: str, msg: str, detail: str = "") -> None:
    if level == "info":
        logger.info(msg)
    elif level == "warning":
        logger.warning(msg)
    else:
        logger.error(msg)
    try:
        await log_error("agent", msg, detail, level)
    except Exception:
        pass


def load_db_settings_to_env() -> None:
    """Load Supabase settings table into os.environ before worker starts."""
    url = os.getenv("SUPABASE_URL", "")
    # Support both key names for compatibility (SUPABASE_SERVICE_ROLE_KEY is canonical)
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "") or os.getenv("SUPABASE_SERVICE_KEY", "")
    if not url or not key:
        logger.warning("SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY not set — skipping settings load")
        return
    try:
        from supabase import create_client
        client = create_client(url, key)
        result = client.table("settings").select("key, value").execute()
        for row in (result.data or []):
            if row.get("value"):
                os.environ[row["key"]] = row["value"]
        logger.info("Loaded %d settings from Supabase", len(result.data or []))
    except Exception as exc:
        logger.warning("Could not load settings from Supabase: %s", exc)


# ── Google Gemini Realtime (Live API) plugin import ───────────────────────────
# Uses Google AI Studio key (GOOGLE_API_KEY) — NOT Vertex AI credentials.
# Supported live models (verified via models.list): gemini-2.5-flash-native-audio-latest, gemini-3.1-flash-live-preview
# Full list: https://docs.livekit.io/agents/integrations/google/

# Live (bidiGenerateContent) models verified to exist via models.list.
VALID_LIVE_MODELS = {
    "gemini-2.5-flash-native-audio-latest",
    "gemini-2.5-flash-native-audio-preview-09-2025",
    "gemini-2.5-flash-native-audio-preview-12-2025",
    "gemini-3.1-flash-live-preview",
}
# Known-bad / stale names that have appeared in env or the settings table,
# mapped to the closest valid live model. These 404 on bidiGenerateContent.
LIVE_MODEL_ALIASES = {
    "gemini-2.5-flash-native-audio-preview": "gemini-2.5-flash-native-audio-latest",
    "gemini-2.5-flash-native-audio": "gemini-2.5-flash-native-audio-latest",
    "gemini-2.0-flash": "gemini-2.5-flash-native-audio-latest",
    "gemini-3.1-flash-live-preview-latest": "gemini-3.1-flash-live-preview",
}
_FALLBACK_LIVE_MODEL = "gemini-2.5-flash-native-audio-latest"


def _normalize_live_model(name: str) -> str:
    """Map stale/invalid live model names to a valid one.

    Guards against a bad GEMINI_MODEL value (e.g. the non-existent
    '...-native-audio-preview') in env or the Supabase settings table silently
    404-ing every call. Returns a model known to support bidiGenerateContent.
    """
    name = (name or "").strip()
    if not name:
        return _FALLBACK_LIVE_MODEL
    if name in VALID_LIVE_MODELS:
        return name
    if name in LIVE_MODEL_ALIASES:
        corrected = LIVE_MODEL_ALIASES[name]
        logger.warning(
            "GEMINI_MODEL '%s' is not a valid live model; using '%s' instead.",
            name, corrected,
        )
        return corrected
    logger.warning(
        "GEMINI_MODEL '%s' is not in the known live-model list; falling back to '%s'. "
        "Valid: %s",
        name, _FALLBACK_LIVE_MODEL, ", ".join(sorted(VALID_LIVE_MODELS)),
    )
    return _FALLBACK_LIVE_MODEL


def _get_google_realtime_model(voice: str = None, model: str = None, instructions: str = None):
    """Import and construct the Google Gemini multimodal-live model for Google AI Studio.

    Uses GOOGLE_API_KEY (from aistudio.google.com) — not Vertex AI.
    The livekit-plugins-google >= 1.0 exposes RealtimeModel directly under
    livekit.plugins.google.realtime (no longer under .beta).
    """
    chosen_voice = voice or os.getenv("GEMINI_TTS_VOICE", "Aoede")
    # Default to gemini-2.5-flash-native-audio-latest — verified live (bidiGenerateContent) model
    chosen_model = model or os.getenv("GEMINI_MODEL", "gemini-2.5-flash-native-audio-latest")
    chosen_model = _normalize_live_model(chosen_model)
    api_key = os.getenv("GOOGLE_API_KEY", "")

    if not api_key:
        raise RuntimeError(
            "GOOGLE_API_KEY is not set. Get your key from https://aistudio.google.com/app/apikey "
            "and add it to .env or the Supabase settings table."
        )

    logger.info("Initializing Gemini Live model: %s (voice=%s)", chosen_model, chosen_voice)

    # ── Attempt 1: Modern API — livekit-plugins-google >= 1.0 ────────────────
    # Path: livekit.plugins.google.realtime.RealtimeModel
    try:
        from livekit.plugins.google import realtime as google_realtime
        return google_realtime.RealtimeModel(
            model=chosen_model,
            voice=chosen_voice,
            api_key=api_key,
            instructions=instructions,
        )
    except (ImportError, AttributeError):
        pass

    # ── Attempt 2: Direct class import ───────────────────────────────────────
    try:
        from livekit.plugins.google.realtime import RealtimeModel
        return RealtimeModel(
            model=chosen_model,
            voice=chosen_voice,
            api_key=api_key,
            instructions=instructions,
        )
    except (ImportError, AttributeError):
        pass

    # ── Attempt 3: Legacy beta path (older plugin versions) ──────────────────
    try:
        from livekit.plugins.google import beta as google_beta
        return google_beta.realtime.RealtimeModel(
            model=chosen_model,
            voice=chosen_voice,
            api_key=api_key,
            instructions=instructions,
        )
    except (ImportError, AttributeError):
        pass

    try:
        from livekit.plugins.google.beta.realtime import RealtimeModel  # type: ignore
        return RealtimeModel(
            model=chosen_model,
            voice=chosen_voice,
            api_key=api_key,
            instructions=instructions,
        )
    except (ImportError, AttributeError) as exc:
        raise RuntimeError(
            "livekit-plugins-google with Gemini Live (realtime) support is not available. "
            "Run: pip install -U 'livekit-plugins-google>=1.0.0'"
        ) from exc


def _get_pipeline_model():
    """Construct a pipeline-mode (STT+LLM+TTS) agent as fallback."""
    try:
        from livekit.plugins import deepgram, openai as lk_openai
        from livekit.plugins.google import LLM as GoogleLLM
        stt = deepgram.STT(api_key=os.getenv("DEEPGRAM_API_KEY", ""))
        llm_model = GoogleLLM(
            model=os.getenv("GEMINI_PIPELINE_MODEL", "gemini-2.5-flash"),
            api_key=os.getenv("GOOGLE_API_KEY", ""),
        )
        tts = lk_openai.TTS()
        return stt, llm_model, tts
    except Exception as exc:
        raise RuntimeError(f"Pipeline fallback model setup failed: {exc}") from exc


# ── Helper: extract phone/name from SIP participant metadata ─────────────────

def _parse_participant_metadata(participant: rtc.RemoteParticipant) -> dict:
    """Extract phone number and lead name from SIP participant metadata."""
    meta: dict = {}
    try:
        raw = participant.metadata or "{}"
        meta = json.loads(raw)
    except Exception:
        pass

    # Caller's phone number. OUTBOUND calls carry it in the metadata above
    # (stamped by server._dispatch_call). INBOUND calls don't — recover it from
    # the SIP attributes LiveKit sets on the participant, or from the identity,
    # which is "sip_+NUMBER" (underscore) or "sip:+NUMBER@host" (colon).
    if not (meta.get("phone_number") or meta.get("phone")):
        attrs = getattr(participant, "attributes", None) or {}
        sip_num = (
            attrs.get("sip.phoneNumber")
            or attrs.get("sip.from")
            or attrs.get("sip.fromNumber")
            or ""
        )
        if not sip_num:
            identity = participant.identity or ""
            for prefix in ("sip:", "sip_"):
                if identity.startswith(prefix):
                    sip_num = identity[len(prefix):].split("@")[0]
                    break
        if sip_num:
            meta["phone_number"] = sip_num
    return meta


def _detect_call_direction(meta: dict) -> str:
    """Classify the call as 'inbound' or 'outbound'.

    Outbound calls are always created by server._dispatch_call(), which stamps
    explicit JSON metadata containing 'lead_name'/'agent_profile_id' keys (even
    when empty strings). Inbound calls arrive through a LiveKit SIP dispatch
    rule whose static metadata we control — set call_direction='inbound' there
    (see setup_inbound_trunk.py). If that tag is missing for any reason, the
    absence of our outbound dispatch keys is the fallback signal.
    """
    direction = (meta.get("call_direction") or meta.get("direction") or "").strip().lower()
    if direction in ("inbound", "outbound"):
        return direction
    if "lead_name" in meta or "agent_profile_id" in meta:
        return "outbound"
    return "inbound"


# ── LiveKit Agent Entrypoint ──────────────────────────────────────────────────

async def entrypoint(ctx: agents.JobContext) -> None:
    """Main agent entrypoint — called once per inbound/outbound call."""
    from livekit.agents import AutoSubscribe
    await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)
    await _log("info", f"Agent connected to room: {ctx.room.name}")

    # Extract participant metadata (phone, name, profile)
    phone_number: Optional[str] = None
    lead_name: Optional[str] = None
    agent_profile_id: Optional[str] = None
    custom_prompt: Optional[str] = None
    voice: Optional[str] = None
    model: Optional[str] = None
    call_direction: str = "outbound"
    lead_source: str = ""

    # Wait for the SIP participant to join
    participant: Optional[rtc.RemoteParticipant] = None
    for _ in range(30):  # up to 15 seconds
        for p in ctx.room.remote_participants.values():
            participant = p
            break
        if participant:
            break
        await asyncio.sleep(0.5)

    if participant:
        meta = _parse_participant_metadata(participant)
        phone_number = meta.get("phone_number") or meta.get("phone")
        lead_name = meta.get("lead_name") or meta.get("name")
        agent_profile_id = meta.get("agent_profile_id")
        custom_prompt = meta.get("system_prompt")
        voice = meta.get("voice")
        model = meta.get("model")
        call_direction = _detect_call_direction(meta)
        lead_source = meta.get("source") or meta.get("lead_source") or ""
        await _log(
            "info",
            f"Participant: {participant.identity}, phone={phone_number}, "
            f"lead={lead_name}, direction={call_direction}",
        )

    # Load agent profile from DB if specified
    if agent_profile_id:
        try:
            profile = await get_agent_profile(agent_profile_id)
            if profile:
                voice = voice or profile.get("voice")
                model = model or profile.get("model")
                custom_prompt = custom_prompt or profile.get("system_prompt")
                await _log("info", f"Using agent profile: {profile.get('name')}")
        except Exception as exc:
            await _log("warning", f"Could not load agent profile: {exc}")

    # Build system prompt — inbound calls (e.g. routed in from a Google My
    # Business listing) get the front-desk qualification persona instead of
    # the outbound booking script. An explicit per-call/profile custom_prompt
    # always wins over both.
    system_prompt = build_prompt(
        lead_name=lead_name or "there",
        business_name=os.getenv("BUSINESS_NAME", "our company"),
        service_type=os.getenv("SERVICE_TYPE", "our service"),
        custom_prompt=custom_prompt,
        call_direction=call_direction,
    )

    # Build tool context
    tools_ctx = AppointmentTools(
        ctx, phone_number=phone_number, lead_name=lead_name,
        call_direction=call_direction, lead_source=lead_source,
    )
    enabled_tools = await get_enabled_tools()
    tool_list = tools_ctx.build_tool_list(enabled_tools)
    tools_ctx._tools = tool_list

    # Set up recording (Egress) if S3 configured
    recording_url: Optional[str] = None
    s3_bucket = os.getenv("S3_BUCKET", "")
    if s3_bucket:
        try:
            recording_url = await _start_recording(ctx, phone_number)
            tools_ctx.recording_url = recording_url
        except Exception as exc:
            await _log("warning", f"Recording start failed: {exc}")

    # ── Build the AI model ────────────────────────────────────────────────────
    use_realtime = os.getenv("USE_GEMINI_REALTIME", "true").lower() in ("true", "1", "yes")

    if use_realtime:
        try:
            realtime_model = _get_google_realtime_model(voice=voice, model=model, instructions=system_prompt)
            session = AgentSession(
                llm=realtime_model,
            )
            agent = GreetingAgent(instructions=system_prompt, tools=tool_list)
            await session.start(
                agent=agent,
                room=ctx.room,
            )
            await _log("info", "Gemini Live realtime session started")
            # The agent greets from GreetingAgent.on_enter once the session is
            # actually running — no premature generate_reply() here.
            await _wait_until_call_ends(ctx)
        except Exception as exc:
            await _log("error", f"Realtime session error: {exc}", str(exc))
        finally:
            try:
                await session.aclose()
            except Exception:
                pass
    else:
        # Pipeline fallback
        try:
            stt, llm_model, tts = _get_pipeline_model()
            vad = silero.VAD.load()
            session = AgentSession(
                stt=stt,
                llm=llm_model,
                tts=tts,
                vad=vad,
            )
            agent = GreetingAgent(instructions=system_prompt, tools=tool_list)
            await session.start(
                agent=agent,
                room=ctx.room,
            )
            await _log("info", "Pipeline (STT+LLM+TTS) session started")
            # The agent greets from GreetingAgent.on_enter once the session is
            # actually running — no premature generate_reply() here.
            await _wait_until_call_ends(ctx)
        except Exception as exc:
            await _log("error", f"Pipeline session error: {exc}", str(exc))
        finally:
            try:
                await session.aclose()
            except Exception:
                pass


async def _wait_until_call_ends(ctx: agents.JobContext) -> None:
    """Block until the remote (SIP) participant disconnects or the room closes.

    Replaces the old session.wait() call, which does not exist in
    livekit-agents 1.x. Keeps the agent process alive for the duration of the
    call so the conversation can actually happen.
    """
    done = asyncio.Event()

    def _on_disconnect(*_args) -> None:
        done.set()

    ctx.room.on("participant_disconnected", _on_disconnect)
    ctx.room.on("disconnected", _on_disconnect)

    try:
        # If there are already no remote participants, end promptly.
        while not done.is_set():
            if len(ctx.room.remote_participants) == 0:
                # Give a brief grace period in case the SIP participant is mid-join
                await asyncio.sleep(1.0)
                if len(ctx.room.remote_participants) == 0:
                    break
            try:
                await asyncio.wait_for(done.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                continue
    finally:
        try:
            ctx.room.off("participant_disconnected", _on_disconnect)
            ctx.room.off("disconnected", _on_disconnect)
        except Exception:
            pass


async def _start_recording(ctx: agents.JobContext, phone_number: Optional[str]) -> Optional[str]:
    """Start LiveKit Egress S3 recording for the call room."""
    bucket = os.getenv("S3_BUCKET", "")
    endpoint = os.getenv("S3_ENDPOINT_URL", "")
    region = os.getenv("S3_REGION", "us-east-1")
    access_key = os.getenv("S3_ACCESS_KEY_ID", "")
    secret_key = os.getenv("S3_SECRET_ACCESS_KEY", "")
    if not all([bucket, access_key, secret_key]):
        return None

    lk_api = api.LiveKitAPI(
        url=os.getenv("LIVEKIT_URL", ""),
        api_key=os.getenv("LIVEKIT_API_KEY", ""),
        api_secret=os.getenv("LIVEKIT_API_SECRET", ""),
    )
    safe_phone = (phone_number or "unknown").replace("+", "").replace(" ", "")
    from datetime import datetime
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filepath = f"recordings/{safe_phone}_{ts}.mp4"

    try:
        egress_req = api.RoomCompositeEgressRequest(
            room_name=ctx.room.name,
            layout="speaker",
            audio_only=True,
            file_outputs=[
                api.EncodedFileOutput(
                    file_type=api.EncodedFileType.MP4,
                    filepath=filepath,
                    s3=api.S3Upload(
                        bucket=bucket,
                        region=region,
                        access_key=access_key,
                        secret=secret_key,
                        endpoint=endpoint or None,
                    ),
                )
            ],
        )
        resp = await lk_api.egress.start_room_composite_egress(egress_req)
        logger.info("Egress started: %s → s3://%s/%s", resp.egress_id, bucket, filepath)
        return f"s3://{bucket}/{filepath}"
    except Exception as exc:
        logger.warning("Egress recording failed to start: %s", exc)
        return None
    finally:
        await lk_api.aclose()


# ── Worker Startup ────────────────────────────────────────────────────────────

def main():
    load_db_settings_to_env()
    init_db()
    agents.cli.run_app(
        agents.WorkerOptions(
            entrypoint_fnc=entrypoint,
            agent_name="outbound-ai",
        )
    )


if __name__ == "__main__":
    main()
