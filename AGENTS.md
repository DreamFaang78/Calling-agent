# AGENTS.md — Project context for AI coding assistants

> Read this first. It is the condensed source of truth for working on this repository.
> For full detail see `ARCHITECTURE.md`. For deployment see `DEPLOYMENT_COOLIFY.md`.

## What this is
An AI phone-calling platform — both directions:
- **Outbound:** dials real numbers over a SIP trunk, runs a booking script, logs outcomes.
- **Inbound:** answers calls routed in from a business number (e.g. listed on Google My
  Business), runs a natural front-desk discovery conversation, qualifies the caller, books
  them if they're ready, and ALWAYS saves a structured `leads` record + texts the owner.

One worker (`agent.py`, `agent_name="outbound-ai"`) handles both — it detects call
direction from SIP dispatch metadata and swaps persona/prompt accordingly (see
`agent._detect_call_direction()` and `prompts.INBOUND_SYSTEM_PROMPT`). Setup for the
inbound side lives in `setup_inbound_trunk.py` + `INBOUND_SETUP.md`.

Everything talks to a Gemini Live realtime LLM, transfers to humans, and logs to
Supabase. A web dashboard drives single calls, CSV batches, scheduled campaigns, and
now a **Leads** page for inbound qualification data.

## Two codebases — know which one you are editing
- **`/` (root) = OutboundAI = PRODUCTION.** FastAPI + LiveKit Agents + Gemini Live + Supabase
  + static HTML dashboard. **This is what gets deployed.** Default to working here.
- **`/LivekitAIVoice` = older prototype.** LiveKit + OpenAI/Groq + Next.js. Reference only;
  not deployed. Its `create_trunk.py`/`list_trunks.py`/`setup_trunk.py` are useful for SIP
  trunk setup. Do not mix its code with the root app.

## Root file map (what to touch for what)
| File | Responsibility | Edit it when… |
|------|----------------|---------------|
| `agent.py` | LiveKit agent worker; runs the live call, picks Gemini realtime vs pipeline, detects inbound vs outbound, starts recording | Changing call behavior, models, recording, direction routing |
| `server.py` | FastAPI REST API + APScheduler campaign runner; dispatches outbound calls via LiveKit SIP, serves `/leads` | Adding/altering endpoints, campaign logic, dispatch |
| `db.py` | All Supabase access (settings, calls, appointments, leads, campaigns, memory, profiles, errors) | Any data read/write or new table access |
| `tools.py` | Function tools the LLM calls mid-call (book, transfer, SMS, memory, Cal.com, **capture_lead**) | Adding/altering what the agent can DO |
| `prompts.py` | System prompt templates + `build_prompt()` — `DEFAULT_SYSTEM_PROMPT` (outbound) and `INBOUND_SYSTEM_PROMPT` (front-desk qualification) | Changing the agent persona / call flow for either direction |
| `setup_inbound_trunk.py` | One-time LiveKit provisioning: inbound SIP trunk + dispatch rule that tags calls `call_direction=inbound` | Adding/changing inbound numbers or channels |
| `ui/index.html` | Single-file dashboard (HTML/CSS/vanilla JS + Chart.js CDN, no build step) — includes the **Leads** page | Any frontend change |
| `supabase_schema.sql` | Idempotent DDL for all tables, incl. `leads` | Adding/changing tables or columns |
| `Dockerfile` | python:3.11-slim image with audio libs; runs `start.sh` | Build/runtime/system deps |
| `start.sh` | Launches uvicorn (`:8000`) AND `python agent.py start` together | Process startup changes |
| `requirements.txt` | Python deps | Dependency changes |

## Runtime model
- One container runs **two processes** (see `start.sh`):
  1. `uvicorn server:app --host 0.0.0.0 --port 8000` (API + dashboard at `/ui`)
  2. `python agent.py start` (LiveKit worker, no inbound port)
- The agent registers with `agent_name="outbound-ai"`. `server.py` dispatches to that exact
  name. **Keep these two strings in sync.** (Prototype uses `outbound-caller` — different.)

## Call flow (memorize this)

**Outbound:**
1. Dashboard → `POST /call/single` → `server._dispatch_call()`.
2. `_dispatch_call` creates a LiveKit room, an agent dispatch (`outbound-ai`), and a SIP
   participant via `OUTBOUND_TRUNK_ID` (this dials the phone) — stamping metadata with
   `phone_number`/`lead_name`/`agent_profile_id`/`system_prompt` keys.
3. `agent.entrypoint()` joins, reads that metadata, detects `direction=outbound`
   (`agent._detect_call_direction()`), loads profile + `DEFAULT_SYSTEM_PROMPT` + tools.
4. Builds Gemini Live model (or pipeline fallback if `USE_GEMINI_REALTIME=false`), starts
   `AgentSession`. LLM follows the booking script, calls `tools.py` functions as needed.
5. `end_call()` writes to `call_logs` and disconnects.

**Inbound** (e.g. a number listed on Google My Business — see `INBOUND_SETUP.md`):
1. Caller dials the business number → SIP provider forwards to LiveKit → the
   `INBOUND_TRUNK_ID` trunk + dispatch rule (provisioned once via
   `setup_inbound_trunk.py`) auto-creates a room and dispatches `outbound-ai`,
   stamping metadata `{"call_direction": "inbound", "source": "google_my_business"}`.
2. `agent.entrypoint()` detects `direction=inbound`, loads `prompts.INBOUND_SYSTEM_PROMPT`
   (front-desk persona — greets as the business, doesn't assume it knows who's calling).
3. The agent has a natural discovery conversation, calls `capture_lead(...)` to persist
   to the `leads` table (regardless of whether they book), which fires a background SMS
   to `LEAD_ALERT_PHONE_NUMBER` via the existing Twilio config.
4. If the caller is ready, the SAME `check_availability`/`book_appointment` flow runs.
5. `end_call()` logs the outcome as usual; captured leads surface on the dashboard's
   **Leads** page (`GET /leads`, `PATCH /leads/{id}/status`).

## Conventions
- **Config comes from env vars AND the Supabase `settings` table.** `agent.py` loads the
  settings table into `os.environ` at startup, so dashboard edits override env without redeploy.
- **Never hardcode secrets.** Everything reads from `os.getenv`. `db.py` redacts sensitive keys.
- `db.py` exposes async functions (`_adb()`); call them with `await`.
- The dashboard calls the backend same-origin (`const API = ''`).
- Python 3.11. Async throughout (FastAPI, LiveKit, Supabase async client).

## CRITICAL gotchas (verify before deploying or debugging DB)
1. **Supabase key name mismatch.** `db.py` reads `SUPABASE_SERVICE_ROLE_KEY`. The sample
   `.env` uses `SUPABASE_SERVICE_KEY`. Use `SUPABASE_SERVICE_ROLE_KEY` (set both if unsure).
   If the DB "silently does nothing", this is almost always why.
2. **`OUTBOUND_TRUNK_ID` must exist** in LiveKit. Create it with
   `LivekitAIVoice/create_trunk.py` then `list_trunks.py`. Without it, `/call/*` returns 500.
3. **`DB_PATH` in `Dockerfile`** is dead SQLite leftover — ignore/remove.
4. **No authentication** on the API/dashboard. Do not expose publicly without putting auth in
   front of it.

## Required environment variables (canonical names)
```
LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET
GOOGLE_API_KEY, GEMINI_MODEL, GEMINI_TTS_VOICE, USE_GEMINI_REALTIME
VOBIZ_SIP_DOMAIN, VOBIZ_USERNAME, VOBIZ_PASSWORD, VOBIZ_OUTBOUND_NUMBER,
OUTBOUND_TRUNK_ID, DEFAULT_TRANSFER_NUMBER
SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY
```
Inbound (see `INBOUND_SETUP.md` for the full walkthrough):
```
INBOUND_TRUNK_ID            # printed by setup_inbound_trunk.py
LEAD_ALERT_PHONE_NUMBER     # owner's number — texted on every captured lead (needs TWILIO_*)
BUSINESS_NAME, SERVICE_TYPE # used in both the inbound greeting and lead alert SMS
```
Optional: `DEEPGRAM_API_KEY`, `TWILIO_*`, `CALCOM_*`, `S3_*`. Full list in `ARCHITECTURE.md` §7.

## How to run / verify
```bash
pip install -r requirements.txt          # deps
# put env vars in .env (see ARCHITECTURE.md §7)
# run supabase_schema.sql in Supabase SQL editor (one time)
uvicorn server:app --host 0.0.0.0 --port 8000   # terminal 1
python agent.py start                            # terminal 2
# open http://localhost:8000/ui ; GET /health should return ok
```
There is no test suite. Verify changes by hitting endpoints (e.g. `GET /health`, `GET /stats`)
and watching agent logs during a test call.

## Do / Don't
- DO keep changes inside the root app unless explicitly working on the prototype.
- DO use `db.py` helpers rather than new Supabase clients.
- DO keep `agent_name` consistent between `agent.py` and `server.py`.
- DON'T commit `.env` (it's gitignored).
- DON'T add SQLite/local-file persistence — the app is Supabase-backed.
- DON'T expose the dashboard publicly without auth.
