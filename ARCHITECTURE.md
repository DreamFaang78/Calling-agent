# OutboundAI — Architecture Reference

> A complete, file-by-file map of this AI voice calling platform. Read this top-to-bottom
> to understand how every piece fits together before changing or deploying anything.

---

## 1. What this project is

An **AI-powered outbound calling platform**. It dials real phone numbers, holds a natural
voice conversation using a realtime LLM, books appointments, transfers to humans, and logs
everything to a database. A web dashboard drives single calls, CSV batches, and scheduled
campaigns.

There are actually **two independent codebases** in this repository:

| Path | Name | Status | Stack | Use it for |
|------|------|--------|-------|------------|
| `/` (root) | **OutboundAI** | **Primary / production** | FastAPI + LiveKit Agents + Google Gemini Live + Supabase + static HTML dashboard | This is the app you deploy to Coolify |
| `/LivekitAIVoice` | LiveKit Vobiz Agent | Prototype / reference | LiveKit Agents + OpenAI/Groq + Deepgram/Sarvam/Cartesia + Next.js dashboard | An earlier, simpler experiment. Not required for production |

**Everything below documents the root OutboundAI app unless stated otherwise.**
The `LivekitAIVoice` project is summarized separately in section 9.

---

## 2. High-level architecture

```
                    ┌──────────────────────────────────────────────┐
                    │                  USER (browser)               │
                    │            ui/index.html dashboard            │
                    └───────────────────────┬──────────────────────┘
                                             │  HTTP (same origin /ui)
                                             ▼
        ┌────────────────────────────────────────────────────────────────┐
        │                  server.py  —  FastAPI (port 8000)               │
        │  REST API: /call, /campaigns, /appointments, /calls, /contacts,  │
        │  /stats, /settings, /logs, /agent-profiles, /scheduler           │
        │  APScheduler runs scheduled campaigns                            │
        └───────────────┬───────────────────────────────┬─────────────────┘
                        │                                 │
        dispatch call   │                                 │ read/write
        (LiveKit SIP)   ▼                                 ▼
        ┌───────────────────────────┐         ┌──────────────────────────┐
        │      LiveKit Cloud         │         │    Supabase (Postgres)    │
        │  - Rooms                   │         │  settings, call_logs,     │
        │  - Agent dispatch          │         │  appointments, campaigns, │
        │  - SIP trunk (Vobiz)       │         │  contact_memory,          │
        └───────────┬───────────────┘         │  agent_profiles,error_logs│
                    │ joins room              └──────────────────────────┘
                    ▼                                       ▲
        ┌───────────────────────────┐                      │ db.py (async)
        │   agent.py — LiveKit       │──────────────────────┘
        │   Agent worker             │
        │  - Gemini Live realtime    │
        │  - tools.py functions      │
        │  - prompts.py instructions │
        └───────────┬───────────────┘
                    │ PSTN audio via SIP
                    ▼
        ┌───────────────────────────┐
        │   Vobiz SIP trunk → phone  │
        │   (the person being called)│
        └───────────────────────────┘
```

**Two processes run inside one container** (see `start.sh`):
1. `uvicorn server:app` — the API + dashboard (port 8000).
2. `python agent.py start` — the LiveKit agent worker (no inbound port; connects out to LiveKit Cloud).

---

## 3. The call lifecycle (end to end)

This is the single most important flow to understand.

1. **Trigger.** A user clicks "Dial Now" in the dashboard, or a campaign fires. The browser
   calls `POST /call/single` (or `/call/batch`, `/call/csv`) on `server.py`.
2. **Dispatch.** `server.py → _dispatch_call()` connects to LiveKit Cloud and:
   - Creates a unique room (`call-<uuid>`).
   - Creates an **agent dispatch** for `agent_name="outbound-ai"` with JSON metadata
     (phone number, lead name, profile id, custom prompt).
   - Calls **`sip.create_sip_participant`** with `OUTBOUND_TRUNK_ID` — this tells the Vobiz
     SIP trunk to actually dial the phone number and bring it into the room.
3. **Agent joins.** LiveKit dispatches the worker process (`agent.py → entrypoint()`), which
   connects to the same room.
4. **Context load.** The agent reads participant metadata (`_parse_participant_metadata`),
   optionally loads an agent profile from Supabase (`get_agent_profile`), and builds the
   system prompt (`prompts.build_prompt`).
5. **Tools wired up.** `tools.AppointmentTools` is instantiated and filtered by the enabled
   tool list (`get_enabled_tools`).
6. **AI session starts.** By default (`USE_GEMINI_REALTIME=true`) it builds a Google **Gemini
   Live** realtime model and starts an `AgentSession`. If realtime is disabled/unavailable it
   falls back to a **pipeline** (Deepgram STT + Gemini LLM + OpenAI TTS).
7. **Conversation.** The model speaks first, follows the call flow in the prompt, and calls
   tools as needed (`check_availability`, `book_appointment`, `transfer_to_human`, etc.).
8. **Recording (optional).** If S3 vars are set, `_start_recording` starts a LiveKit Egress
   that uploads an MP4 to S3-compatible storage.
9. **End + log.** The model calls `end_call(outcome, reason)`, which writes a row to
   `call_logs` (with duration and recording URL) and disconnects the room.

---

## 4. Root file-by-file reference

### `agent.py` — LiveKit Agent worker (the "voice brain")
The process that actually talks on the phone. Key parts:
- **SSL patch (top of file):** monkey-patches `ssl.create_default_context` to use `certifi`'s
  CA bundle. Must run before any networking imports. Prevents TLS failures.
- **`load_db_settings_to_env()`:** at startup, pulls the `settings` table from Supabase into
  `os.environ`, so config edited in the dashboard takes effect without a redeploy.
- **`_get_google_realtime_model()`:** constructs the Gemini Live realtime model. Tries several
  import paths for `livekit-plugins-google` for version resilience.
- **`_get_pipeline_model()`:** fallback STT+LLM+TTS (Deepgram + Gemini + OpenAI).
- **`_parse_participant_metadata()`:** extracts phone/name from SIP participant metadata.
- **`entrypoint(ctx)`:** the per-call handler described in section 3.
- **`_start_recording()`:** starts LiveKit Egress → S3 (audio-only MP4).
- **`main()`:** loads settings, `init_db()`, runs the worker with `agent_name="outbound-ai"`.

> Note: `agent_name` here is **`outbound-ai`** and must match the `agent_name` used in
> `server.py`'s dispatch call. (The `LivekitAIVoice` prototype uses `outbound-caller` — do not
> confuse the two.)

### `server.py` — FastAPI backend + scheduler
The control plane. Defines all REST endpoints and the campaign scheduler.
- **App + CORS + static mount:** serves `ui/` at `/ui`, allows all origins.
- **Pydantic models:** request bodies for calls, campaigns, appointments, settings, profiles.
- **Lifecycle:** `startup` runs `init_db()`, starts APScheduler, re-registers campaign jobs.
- **`_dispatch_call()`:** the core LiveKit SIP dispatch (section 3, step 2).
- **Endpoints (grouped):**
  - Calls: `POST /call/single`, `/call/batch`, `/call/csv`
  - Campaigns: `GET/POST /campaigns`, `/{id}` get/delete, `/{id}/pause|resume|run`
  - Appointments: `GET/POST /appointments`, `DELETE /appointments/{id}`
  - Call logs: `GET /calls`, `PATCH /calls/{id}/notes`
  - CRM: `GET /contacts`, `GET/POST /contacts/{phone}/memory`
  - Stats: `GET /stats`
  - Settings (BYOK): `GET/POST /settings`, `GET/POST /settings/{key}`
  - Logs: `GET /logs`, `DELETE /logs`
  - Agent profiles: full CRUD under `/agent-profiles`
  - Scheduler: `GET /scheduler/jobs`, `GET /health`
- **Campaign engine:** `_run_campaign` dispatches each contact with a delay;
  `_schedule_campaign` registers APScheduler jobs (`once` / `daily` / `weekdays`).

### `db.py` — Supabase data access layer
All persistence. Uses the Supabase Python client (sync `_sdb()` and async `_adb()`).
- **`DEFAULTS` + `_default()`:** every credential is read from env vars; nothing is hardcoded.
- **`init_db()`:** verifies the Supabase connection; prints a hint to run the SQL schema if it fails.
- **Settings:** `get_all_settings`, `save_settings`, `get_setting`, `set_setting`,
  `get_enabled_tools`. Sensitive keys are never returned by value, only a `configured` boolean.
- **Error logs:** `log_error`, `get_errors`, `get_logs`, `clear_errors`.
- **Appointments:** insert/check-slot/next-available/get/cancel/by-phone.
- **Call logs:** `log_call`, `get_all_calls` (paginated), `get_calls_by_phone`,
  `update_call_notes`, `get_contacts` (derives a CRM view from call history).
- **Stats:** `get_stats` aggregates totals, booking rate, outcome breakdown, 14-day timeline,
  and average duration by outcome.
- **Campaigns:** create/list/get/update-status/run-stats/delete.
- **Contact memory:** `add_contact_memory`, `get_contact_memory`, `compress_contact_memory`
  (long-term per-contact notes the agent reads/writes across calls).
- **Agent profiles:** full CRUD + default-profile management.

> ⚠️ **Known config mismatch:** `db.py` reads `SUPABASE_SERVICE_ROLE_KEY`, but the sample
> `.env` defines `SUPABASE_SERVICE_KEY`. Use **`SUPABASE_SERVICE_ROLE_KEY`** as the canonical
> name (see section 7) or the database layer will silently fail to connect.

### `tools.py` — Agent function tools
`AppointmentTools(llm.ToolContext)` — the functions the LLM can call mid-conversation:
- `check_availability(date, time)` — checks a slot in Supabase.
- `book_appointment(name, phone, date, time, service)` — writes a booking, returns booking id.
- `end_call(outcome, reason)` — logs the call and disconnects. **Always called at the end.**
- `transfer_to_human(reason)` — SIP REFER to `DEFAULT_TRANSFER_NUMBER`.
- `send_sms_confirmation(phone, message)` — Twilio SMS (skips if not configured).
- `lookup_contact(phone)` — pulls prior calls, appointments, and memories at call start.
- `remember_details(insight)` — stores a per-contact note; auto-compresses with Gemini Flash
  once 5+ notes accumulate.
- `book_calcom` / `cancel_calcom` — Cal.com calendar sync (optional).
- `build_tool_list(enabled)` — filters which tools are active per the `ENABLED_TOOLS` setting.

### `prompts.py` — System prompt template
- `DEFAULT_SYSTEM_PROMPT` — the full "Priya" appointment-booking persona: speak-first rule,
  6-step call flow, objection handling, style rules, and tool-usage rules.
- `build_prompt(lead_name, business_name, service_type, custom_prompt)` — interpolates
  variables; a custom prompt (from a profile or request) overrides the default.

### `ui/index.html` — Single-file dashboard
A self-contained dark-themed SPA (HTML + CSS + vanilla JS, Chart.js via CDN). No build step.
- `const API = ''` — calls the backend on the same origin (served at `/ui`, backend on `:8000`).
- Pages: Dashboard (stats + charts), Single Call, Batch CSV, Campaigns, Appointments, CRM,
  Call Logs, Live Logs, AI Prompts, Agent Profiles, Settings (BYOK).
- Talks to every `server.py` endpoint via the `api()` fetch helper.

### `supabase_schema.sql` — Database schema
Idempotent (`IF NOT EXISTS`) DDL for all tables. **Run once** in Supabase → SQL Editor.
Tables: `appointments`, `call_logs`, `settings`, `error_logs`, `campaigns`,
`contact_memory`, `agent_profiles`. Row Level Security is disabled (the service role key is
used server-side only).

### `requirements.txt` — Python dependencies
LiveKit Agents + plugins (google, deepgram, silero, noise-cancellation), FastAPI/uvicorn,
Supabase, APScheduler, Twilio, google-generativeai, certifi, httpx, python-dotenv.

### `Dockerfile` — Container image
`python:3.11-slim`, installs audio system libs (`libgomp1`, `libglib2.0-0`, `libsndfile1`,
`curl`), installs requirements, copies code, exposes `8000`, runs `start.sh`.

> ⚠️ The line `ENV DB_PATH=/data/appointments.db` is a **leftover from an old SQLite version**.
> The app now uses Supabase and ignores it. Harmless, but safe to delete.

### `start.sh` — Container entrypoint
Exports `.env` if present, prints config, starts `uvicorn server:app` on `:8000`, waits 2s,
then starts `python agent.py start`. Waits on both PIDs so the container stays alive.

### `.env` — Secrets (NOT committed; in `.gitignore`)
Holds all credentials. See section 7 for the canonical variable list.

---

## 5. External services this app depends on

| Service | Purpose | Required? | Key env vars |
|---------|---------|-----------|--------------|
| **LiveKit Cloud** | Realtime rooms, agent dispatch, SIP bridging | **Yes** | `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET` |
| **Vobiz (SIP trunk)** | PSTN connectivity (actual phone dialing) | **Yes** | `VOBIZ_SIP_DOMAIN`, `VOBIZ_USERNAME`, `VOBIZ_PASSWORD`, `VOBIZ_OUTBOUND_NUMBER`, `OUTBOUND_TRUNK_ID` |
| **Google Gemini** | Realtime voice LLM (Gemini Live) | **Yes** | `GOOGLE_API_KEY`, `GEMINI_MODEL`, `GEMINI_TTS_VOICE`, `USE_GEMINI_REALTIME` |
| **Supabase** | Postgres database (all app data) | **Yes** | `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY` |
| **Deepgram** | STT for pipeline fallback | Optional | `DEEPGRAM_API_KEY` |
| **Twilio** | SMS confirmations | Optional | `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER` |
| **Cal.com** | Calendar booking sync | Optional | `CALCOM_API_KEY`, `CALCOM_EVENT_TYPE_ID`, `CALCOM_TIMEZONE` |
| **S3 / Supabase Storage** | Call recording storage (Egress) | Optional | `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY`, `S3_ENDPOINT_URL`, `S3_REGION`, `S3_BUCKET` |

---

## 6. Data model (Supabase tables)

- **`settings`** `(key, value, updated_at)` — runtime config, editable from the dashboard.
- **`call_logs`** `(id, phone_number, lead_name, outcome, reason, duration_seconds, timestamp, recording_url, notes)`
- **`appointments`** `(id, name, phone, date, time, service, status, created_at, calcom_booking_uid)`
- **`campaigns`** `(id, name, status, contacts_json, schedule_type, schedule_time, call_delay_seconds, system_prompt, agent_profile_id, created_at, last_run_at, total_dispatched, total_failed)`
- **`contact_memory`** `(id, phone_number, insight, created_at)` — per-contact long-term notes.
- **`agent_profiles`** `(id, name, voice, model, system_prompt, enabled_tools, is_default, created_at)`
- **`error_logs`** `(id, source, level, message, detail, timestamp)`

---

## 7. Canonical environment variables

> Use these exact names. Anything marked **required** must be set for the app to function.
> The agent reads many of these both from env vars and from the Supabase `settings` table
> (dashboard edits override env at agent startup).

```bash
# ── LiveKit Cloud (required) ──
LIVEKIT_URL=wss://your-project.livekit.cloud
LIVEKIT_API_KEY=APIxxxxxxxxxxxxx
LIVEKIT_API_SECRET=xxxxxxxxxxxxxxxxxxxxxxxxxxxx

# ── Google Gemini (required) ──
GOOGLE_API_KEY=AIzaSyxxxxxxxxxxxxxxxxxxxxxxxx
GEMINI_MODEL=gemini-3.1-flash-live-preview
GEMINI_TTS_VOICE=Aoede
USE_GEMINI_REALTIME=true

# ── Vobiz SIP telephony (required) ──
VOBIZ_SIP_DOMAIN=xxxxxxxx.sip.vobiz.ai
VOBIZ_USERNAME=your_username
VOBIZ_PASSWORD=your_password
VOBIZ_OUTBOUND_NUMBER=+919876543210
OUTBOUND_TRUNK_ID=ST_xxxxxxxxxxxxxxxx
DEFAULT_TRANSFER_NUMBER=+919876543210

# ── Supabase (required) ──
SUPABASE_URL=https://xxxxxxxx.supabase.co
SUPABASE_SERVICE_ROLE_KEY=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...

# ── Optional integrations ──
DEEPGRAM_API_KEY=
TWILIO_ACCOUNT_SID=
TWILIO_AUTH_TOKEN=
TWILIO_FROM_NUMBER=
CALCOM_API_KEY=
CALCOM_EVENT_TYPE_ID=
CALCOM_TIMEZONE=Asia/Kolkata
S3_ACCESS_KEY_ID=
S3_SECRET_ACCESS_KEY=
S3_ENDPOINT_URL=
S3_REGION=ap-northeast-1
S3_BUCKET=call-recordings
```

> 🔴 **Action required:** your current `.env` uses `SUPABASE_SERVICE_KEY`. Rename it to
> **`SUPABASE_SERVICE_ROLE_KEY`** (what `db.py` actually reads). Optionally, keep both for
> safety. See `DEPLOYMENT_COOLIFY.md`.

---

## 8. Run locally (for testing)

```bash
# 1. Python deps
pip install -r requirements.txt

# 2. Create .env from section 7

# 3. Create DB tables: paste supabase_schema.sql into Supabase → SQL Editor → Run

# 4. (One-time) create the SIP trunk in LiveKit if you don't have OUTBOUND_TRUNK_ID
#    Use the helper scripts in LivekitAIVoice/ (create_trunk.py / list_trunks.py)

# 5. Start API + dashboard
uvicorn server:app --host 0.0.0.0 --port 8000

# 6. In another terminal, start the agent worker
python agent.py start

# 7. Open http://localhost:8000/ui
```

In production both processes are launched together by `start.sh` inside the Docker container.

---

## 9. The `LivekitAIVoice/` prototype (reference only)

A separate, earlier project. **Not deployed for production**, but useful as a reference and it
contains handy SIP trunk setup scripts.

- **`agent.py`** — LiveKit agent using OpenAI/Groq LLM + Deepgram STT + OpenAI/Sarvam/Cartesia
  TTS. Agent name is **`outbound-caller`**. Reads persona from `config.py`.
- **`config.py`** — central config: system prompt (a "school receptionist" demo), model/voice
  providers, transfer number, SIP trunk id.
- **`make_call.py`** — CLI to dispatch a single outbound call (`python make_call.py --to +91...`).
- **`create_trunk.py` / `setup_trunk.py` / `list_trunks.py`** — **genuinely useful utilities**
  to create, update, and list LiveKit SIP outbound trunks. You can use these to obtain the
  `OUTBOUND_TRUNK_ID` the root app needs.
- **`dashboard/`** — a Next.js 16 UI with `/api/dispatch` and `/api/queue` routes that call
  LiveKit directly via `livekit-server-sdk`. Components: `CallDispatcher`, `BulkDialer`.
- **`Dockerfile` / `docker-compose.yml`** — containerization for this prototype only.
- **`README.md` / `transfer_call.md`** — setup and SIP transfer troubleshooting guides.

**Recommendation:** for production, deploy the **root** app. Keep `LivekitAIVoice` only if you
want its trunk-setup scripts or want to study the Next.js dashboard approach. If you don't need
it, you can delete the folder to avoid confusion.

---

## 10. Known issues / cleanup checklist

1. **`SUPABASE_SERVICE_KEY` vs `SUPABASE_SERVICE_ROLE_KEY`** — rename in `.env`/Coolify so it
   matches what `db.py` reads. (Highest priority — blocks the database.)
2. **`DB_PATH` in `Dockerfile`** — dead SQLite leftover; safe to remove.
3. **Two `agent_name`s** — root app uses `outbound-ai`; prototype uses `outbound-caller`.
   They must each match their own dispatch caller. Don't cross them.
4. **CORS is `*`** — fine behind a single-origin deploy, but tighten if you expose the API
   publicly to other origins.
5. **No auth on the dashboard/API** — `server.py` has no authentication. Anyone who can reach
   the URL can place calls. Put it behind Coolify auth / a reverse-proxy login / IP allowlist
   before exposing it to the internet. (See `DEPLOYMENT_COOLIFY.md`, security section.)
6. **OpenAI TTS in pipeline fallback** requires an OpenAI key that isn't in the env list — the
   realtime Gemini path is the default and intended route.

---

## 11. Related documents

- **`AGENTS.md`** — condensed, machine-readable project context for AI coding tools.
- **`DEPLOYMENT_COOLIFY.md`** — step-by-step deploy to Coolify on a DigitalOcean Ubuntu droplet.
