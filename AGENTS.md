# AGENTS.md — Project context for AI coding assistants

> Read this first. It is the condensed source of truth for working on this repository.
> For full detail see `ARCHITECTURE.md`. For deployment see `DEPLOYMENT_COOLIFY.md`.

## What this is
An AI outbound phone-calling platform. It dials real numbers over a SIP trunk, holds a
realtime voice conversation with a Gemini Live LLM, books appointments, transfers to humans,
and logs everything to Supabase. A web dashboard drives single calls, CSV batches, and
scheduled campaigns.

## Two codebases — know which one you are editing
- **`/` (root) = OutboundAI = PRODUCTION.** FastAPI + LiveKit Agents + Gemini Live + Supabase
  + static HTML dashboard. **This is what gets deployed.** Default to working here.
- **`/LivekitAIVoice` = older prototype.** LiveKit + OpenAI/Groq + Next.js. Reference only;
  not deployed. Its `create_trunk.py`/`list_trunks.py`/`setup_trunk.py` are useful for SIP
  trunk setup. Do not mix its code with the root app.

## Root file map (what to touch for what)
| File | Responsibility | Edit it when… |
|------|----------------|---------------|
| `agent.py` | LiveKit agent worker; runs the live call, picks Gemini realtime vs pipeline, starts recording | Changing call behavior, models, recording |
| `server.py` | FastAPI REST API + APScheduler campaign runner; dispatches calls via LiveKit SIP | Adding/altering endpoints, campaign logic, dispatch |
| `db.py` | All Supabase access (settings, calls, appointments, campaigns, memory, profiles, errors) | Any data read/write or new table access |
| `tools.py` | Function tools the LLM calls mid-call (book, transfer, SMS, memory, Cal.com) | Adding/altering what the agent can DO |
| `prompts.py` | System prompt template + `build_prompt()` | Changing the agent persona / call flow |
| `ui/index.html` | Single-file dashboard (HTML/CSS/vanilla JS + Chart.js CDN, no build step) | Any frontend change |
| `supabase_schema.sql` | Idempotent DDL for all tables | Adding/changing tables or columns |
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
1. Dashboard → `POST /call/single` → `server._dispatch_call()`.
2. `_dispatch_call` creates a LiveKit room, an agent dispatch (`outbound-ai`), and a SIP
   participant via `OUTBOUND_TRUNK_ID` (this dials the phone).
3. `agent.entrypoint()` joins the room, reads metadata, loads profile + prompt + tools.
4. Builds Gemini Live model (or pipeline fallback if `USE_GEMINI_REALTIME=false`), starts
   `AgentSession`.
5. LLM follows `prompts.DEFAULT_SYSTEM_PROMPT`, calls `tools.py` functions as needed.
6. `end_call()` writes to `call_logs` and disconnects.

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
