# OutboundAI — Architecture Reference & Recent Updates

This document describes the high-level architecture of OutboundAI and summarizes the recent updates made to the codebase to maintain compatibility, stability, and dashboard observability.

---

## 1. System Overview

OutboundAI is an **AI-powered outbound voice calling platform**. It integrates several key systems:
- **FastAPI / server.py (Control Plane):** Manages API endpoints for dialing single numbers, loading CSV batch jobs, monitoring campaign states, handling agent configurations, and serving the static dashboard interface.
- **LiveKit Agents / agent.py (Voice Brain):** Runs inside the worker process to interface with the LiveKit Room, handle audio processing via WebRTC, and manage AI session lifecycles.
- **Google Gemini Live (Realtime Voice Model):** Powering bidirectional speech conversations with low latency.
- **Supabase (Persistence & Logging):** Storage layer for appointments, call outcomes, custom agent profiles, error logs, and persistent settings.

---

## 2. Recent Updates Summary

Here are the latest commits that have been successfully fetched and updated in the project:

### Commit History

1. **`861be1b` — Instrument `on_enter` greeting with DB-backed logging to surface why agent is silent**
   - **Problem:** Dashboard live logs rely on the database-backed logging, whereas typical `logger.warning`/`logger.info` outputs only write to container stdout.
   - **Solution:** Configured the `GreetingAgent.on_enter` greeting to log its entry and exit statuses using the internal `_log()` helper to the `error_logs` table. Awaited the `SpeechHandle` so any generation/audio pipeline failures are caught and surfaced directly on the dashboard.

2. **`f373843` — Ignore local diagnostic scripts that contain hardcoded API keys**
   - **Solution:** Appended diagnostic/temporary scripts (`diag_key_check.py`, `diag_startup_sim.py`, `fix_settings.py`) to `.gitignore` to prevent sensitive credentials from leaking to git histories.

3. **`3e22820` — DB: align agent_profiles model default + add performance indexes; add supabase diagnostic**
   - **Solution:** Updated the default value for models in `supabase_schema.sql` to `gemini-2.5-flash-native-audio-latest` (to prevent retired model naming mismatches).
   - **Solution:** Added `supabase_migration_indexes.sql` containing idempotent database indexes on `call_logs`, `appointments`, `error_logs`, and `campaigns` to optimize layout queries.
   - **Solution:** Provided `diag_supabase.py` to securely log row counts and setting overrides on startup.

4. **`d40eabb` — Add live model-name guard: auto-correct invalid GEMINI_MODEL**
   - **Solution:** Implemented `_normalize_live_model()` within `agent.py`. It checks incoming model configs and maps stale preview names (e.g., `gemini-2.5-flash-native-audio-preview` or `gemini-2.0-flash`) to the closest available live model (`gemini-2.5-flash-native-audio-latest`) so runtime sessions don't 404.

5. **`0d3aa1b` & `b24499c` — Fix silent agent race conditions & disconnected sessions**
   - **Problem:** The agent disconnected prematurely and sometimes failed to speak its opening greeting.
   - **Solution:** Extracted the greeting trigger into a dedicated `GreetingAgent(Agent)` class that overrides `on_enter`. Replaced `session.wait()` with `_wait_until_call_ends(ctx)` to correctly keep the agent alive during the call. 
   - **Update (`agent.py`):** Explicitly added `instructions` to `generate_reply(instructions="...")` so the Gemini Realtime model proactively speaks the greeting without waiting for the user to speak first.

6. **`fd94bd2` & `7c92994` & `b7d5016` — Fix Gemini integration bugs and Tool passing**
   - **Solution:** Switched the memory compression backend from the deprecated `google-generativeai` package to the official `google-genai` async client wrapper.
   - **Solution:** Passed `tool_list` directly to `Agent(tools=tool_list)` instead of passing a `ToolContext` object, resolving realtime session unsupported operand type crashes.

7. **Inbound Call Handling**
   - **Update (`agent.py`):** The `entrypoint` function now parses the SIP `participant.identity` to dynamically extract the inbound caller's phone number, enabling the agent to gracefully handle and answer inbound calls routed to the `outbound-ai` LiveKit agent.

---

## 3. High-Level Architecture & Flow

```
                     ┌──────────────────────────────────────────────┐
                     │                  USER (browser)               │
                     │            ui/index.html dashboard            │
                     └───────────────────────┬──────────────────────┘
                                              │  HTTP / WebSockets
                                              ▼
         ┌────────────────────────────────────────────────────────────────┐
         │                  server.py  —  FastAPI (port 8000)               │
         │  REST API: /call, /campaigns, /appointments, /calls, /settings  │
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

### End-To-End Outbound Call Pipeline
1. **Initiation:** The Web UI or campaign engine triggers `POST /call/single` in `server.py`.
2. **Room & Participant Allocation:** FastAPI hooks into the LiveKit API to provision a call-specific room and creates a SIP participant targeting the dial destination.
3. **Agent Activation:** The LiveKit Agent Framework instantiates `agent.py` to handle the worker logic, spawning a custom `GreetingAgent` context.
4. **Session Startup:**
   - **Realtime Mode (Default):** Hooks up the bidirectional Gemini live audio channel via `bidiGenerateContent`.
   - **Pipeline Mode (Fallback):** Spawns Deepgram STT, Gemini LLM, and OpenAI TTS blocks sequentially.
5. **Greeting Hook:** Once the session transitions to the active room context, `GreetingAgent.on_enter()` calls `session.generate_reply()`, prompting immediate audio output to greet the lead.
6. **Interaction Loop:** The conversational pipeline responds to voice inputs, invoking functions mapped in `tools.py` (e.g. appointment booking, Cal.com scheduling, lookup, or SIP routing transfers).
7. **Graceful Outro:** On completion, the agent runs `end_call(outcome, reason)`, writes the metadata to the Supabase database, and disconnects.
