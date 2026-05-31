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

def _get_google_realtime_model(voice: str = None, model: str = None):
    """Import and construct the Google Gemini multimodal-live model for Google AI Studio.

    Uses GOOGLE_API_KEY (from aistudio.google.com) — not Vertex AI.
    The livekit-plugins-google >= 1.0 exposes RealtimeModel directly under
    livekit.plugins.google.realtime (no longer under .beta).
    """
    chosen_voice = voice or os.getenv("GEMINI_TTS_VOICE", "Aoede")
    # Default to gemini-2.5-flash-native-audio-latest — verified live (bidiGenerateContent) model
    chosen_model = model or os.getenv("GEMINI_MODEL", "gemini-2.5-flash-native-audio-latest")
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
        )
    except (ImportError, AttributeError):
        pass

    try:
        from livekit.plugins.google.beta.realtime import RealtimeModel  # type: ignore
        return RealtimeModel(
            model=chosen_model,
            voice=chosen_voice,
            api_key=api_key,
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
            model=os.getenv("GEMINI_MODEL", "gemini-2.0-flash"),
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
    # SIP identity is typically sip:+NUMBER@domain
    identity = participant.identity or ""
    if "sip:" in identity:
        num_part = identity.replace("sip:", "").split("@")[0]
        if num_part and "phone_number" not in meta:
            meta["phone_number"] = num_part
    return meta


# ── LiveKit Agent Entrypoint ──────────────────────────────────────────────────

async def entrypoint(ctx: agents.JobContext) -> None:
    """Main agent entrypoint — called once per inbound/outbound call."""
    await ctx.connect()
    await _log("info", f"Agent connected to room: {ctx.room.name}")

    # Extract participant metadata (phone, name, profile)
    phone_number: Optional[str] = None
    lead_name: Optional[str] = None
    agent_profile_id: Optional[str] = None
    custom_prompt: Optional[str] = None
    voice: Optional[str] = None
    model: Optional[str] = None

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
        await _log("info", f"Participant: {participant.identity}, phone={phone_number}, lead={lead_name}")

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

    # Build system prompt
    system_prompt = build_prompt(
        lead_name=lead_name or "there",
        business_name=os.getenv("BUSINESS_NAME", "our company"),
        service_type=os.getenv("SERVICE_TYPE", "our service"),
        custom_prompt=custom_prompt,
    )

    # Build tool context
    tools_ctx = AppointmentTools(ctx, phone_number=phone_number, lead_name=lead_name)
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
            realtime_model = _get_google_realtime_model(voice=voice, model=model)
            session = AgentSession(
                llm=realtime_model,
            )
            agent = Agent(instructions=system_prompt, tools=tool_list)
            await session.start(
                agent=agent,
                room=ctx.room,
                room_input_options=RoomInputOptions(
                    noise_cancellation=noise_cancellation.BVC(),
                ),
            )
            await _log("info", "Gemini Live realtime session started")
            await session.wait()
        except Exception as exc:
            await _log("error", f"Realtime session error: {exc}", str(exc))
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
            agent = Agent(instructions=system_prompt, tools=tool_list)
            await session.start(
                agent=agent,
                room=ctx.room,
                room_input_options=RoomInputOptions(
                    noise_cancellation=noise_cancellation.BVC(),
                ),
            )
            await _log("info", "Pipeline (STT+LLM+TTS) session started")
            await session.wait()
        except Exception as exc:
            await _log("error", f"Pipeline session error: {exc}", str(exc))


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
