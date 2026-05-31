# Deploying OutboundAI to Coolify on a DigitalOcean Ubuntu server

> This guide deploys the **root** app (OutboundAI). The `LivekitAIVoice/` folder is not
> deployed. Follow the steps in order. Total time ~30–45 min.

---

## 0. What you are deploying

A single Docker container that runs two processes:
- `uvicorn server:app` on **port 8000** — the REST API + dashboard (`/ui`)
- `python agent.py start` — the LiveKit agent worker (outbound only, no inbound port)

The container is defined by the root `Dockerfile` + `start.sh`. Coolify builds the image from
your Git repo and runs it, injecting environment variables you configure in the UI.

---

## 1. Prerequisites (accounts + values to collect)

Before touching the server, gather every credential. Put them in a scratch file locally.

| You need | Where to get it | Variable(s) |
|----------|-----------------|-------------|
| LiveKit Cloud project | cloud.livekit.io → Project → Settings → Keys | `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET` |
| LiveKit SIP outbound trunk | created in step 5 below | `OUTBOUND_TRUNK_ID` |
| Vobiz SIP credentials | vobiz.ai dashboard | `VOBIZ_SIP_DOMAIN`, `VOBIZ_USERNAME`, `VOBIZ_PASSWORD`, `VOBIZ_OUTBOUND_NUMBER` |
| Google Gemini API key | aistudio.google.com/app/apikey | `GOOGLE_API_KEY` |
| Supabase project | supabase.com → Project → Settings → API | `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY` (the **service_role** secret, not anon) |
| (optional) Deepgram | console.deepgram.com | `DEEPGRAM_API_KEY` |
| (optional) Twilio | twilio.com console | `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER` |
| (optional) Cal.com | cal.com → Settings → API Keys | `CALCOM_API_KEY`, `CALCOM_EVENT_TYPE_ID`, `CALCOM_TIMEZONE` |
| (optional) S3 recordings | Supabase Storage S3 / AWS | `S3_*` vars |

You also need:
- A **DigitalOcean account**.
- A **GitHub/GitLab repo** containing this project (Coolify deploys from Git). Make sure `.env`
  is **not** committed (it's already in `.gitignore`).
- A **domain name** (recommended) to point at the server for HTTPS.

---

## 2. Create the DigitalOcean droplet

1. DigitalOcean → Create → Droplets.
2. Image: **Ubuntu 24.04 LTS** (22.04 also fine).
3. Size: at least **2 vCPU / 4 GB RAM** (Basic Regular or Premium). Voice + two processes +
   Coolify itself need headroom; 2 GB is too tight.
4. Choose a region close to your callers / LiveKit region for lower latency.
5. Authentication: add your **SSH key** (preferred).
6. Hostname: e.g. `outboundai-prod`. Create the droplet and note its **public IP**.

(Optional but recommended) Point your domain's A record to this IP now, e.g.
`app.yourdomain.com → <droplet-ip>`.

---

## 3. Install Coolify on the droplet

SSH in and run the official installer:

```bash
ssh root@<droplet-ip>

# update system
apt update && apt upgrade -y

# install Coolify (installs Docker + Coolify automatically)
curl -fsSL https://cdn.coollabs.io/coolify/install.sh | bash
```

When it finishes, open `http://<droplet-ip>:8000` in your browser and create the Coolify admin
account. (Coolify's own UI uses port 8000 on the host, but that's the host-level Coolify panel;
your app container's port 8000 is mapped separately by Coolify's proxy — no conflict.)

---

## 4. Set up Supabase (database)

1. In Supabase, open your project → **SQL Editor** → New query.
2. Paste the entire contents of `supabase_schema.sql` and **Run**. It is idempotent
   (`IF NOT EXISTS`), so re-running is safe.
3. Confirm tables exist under **Table Editor**: `settings`, `call_logs`, `appointments`,
   `campaigns`, `contact_memory`, `agent_profiles`, `error_logs`.
4. Copy **Project URL** → `SUPABASE_URL` and the **service_role** key →
   `SUPABASE_SERVICE_ROLE_KEY`.

> ⚠️ Use the **service_role** secret key, not the public `anon` key. The backend needs full
> access and runs server-side only.

---

## 5. Create the LiveKit SIP outbound trunk (one time)

The app needs `OUTBOUND_TRUNK_ID`. If you don't have one yet, use the helper scripts in
`LivekitAIVoice/` from your **local machine** (they only need the LiveKit + Vobiz vars):

```bash
cd LivekitAIVoice
python -m venv venv && source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt

# create a .env in LivekitAIVoice/ with LIVEKIT_* and VOBIZ_* values, then:
python create_trunk.py     # prints the new Trunk ID  (ST_xxxxxxxx)
python list_trunks.py      # lists trunks to confirm
```

Copy the printed `ST_...` value into `OUTBOUND_TRUNK_ID`.

> If a trunk already exists but auth fails, `setup_trunk.py` re-syncs the Vobiz username/password
> onto the existing trunk (fixes "max auth retry" errors).

Also ensure **Call Transfer (SIP REFER)** is enabled in your Vobiz dashboard if you want the
`transfer_to_human` tool to work.

---

## 6. Create the application in Coolify

1. Coolify → **Projects** → New Project → New Resource → **Application**.
2. Source: connect your **GitHub/GitLab** account and pick this repository + branch.
3. Build Pack: select **Dockerfile** (Coolify will use the root `Dockerfile`).
4. **Base Directory:** `/` (root). Do **not** point it at `LivekitAIVoice`.
5. **Port:** set the exposed port to **8000** (matches `EXPOSE 8000` and uvicorn).
6. Health check (optional but recommended): path `/health`, port `8000`.

---

## 7. Configure environment variables in Coolify

In the application's **Environment Variables** tab, add each variable below. **Do not upload a
`.env` file** — set them here so they're injected at runtime.

```bash
# ── LiveKit (required) ──
LIVEKIT_URL=wss://your-project.livekit.cloud
LIVEKIT_API_KEY=APIxxxxxxxxxxxxx
LIVEKIT_API_SECRET=xxxxxxxxxxxxxxxxxxxxxxxxxxxx

# ── Gemini (required) ──
GOOGLE_API_KEY=AIzaSyxxxxxxxxxxxxxxxxxxxxxxxx
GEMINI_MODEL=gemini-2.5-flash-native-audio-latest
GEMINI_TTS_VOICE=Aoede
USE_GEMINI_REALTIME=true

# ── Vobiz SIP (required) ──
VOBIZ_SIP_DOMAIN=xxxxxxxx.sip.vobiz.ai
VOBIZ_USERNAME=your_username
VOBIZ_PASSWORD=your_password
VOBIZ_OUTBOUND_NUMBER=+919876543210
OUTBOUND_TRUNK_ID=ST_xxxxxxxxxxxxxxxx
DEFAULT_TRANSFER_NUMBER=+919876543210

# ── Supabase (required) ──
SUPABASE_URL=https://xxxxxxxx.supabase.co
SUPABASE_SERVICE_ROLE_KEY=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...

# ── Optional ──
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

> 🔴 **Critical:** the variable name must be **`SUPABASE_SERVICE_ROLE_KEY`** — that's what
> `db.py` reads. Your sample `.env` calls it `SUPABASE_SERVICE_KEY`, which the app ignores.
> If unsure, set **both** names to the same value to be safe.

---

## 8. Deploy

1. Click **Deploy** in Coolify. Watch the build logs — it builds the Docker image, installs
   the audio libs and Python deps, then runs `start.sh`.
2. On success, the runtime logs should show:
   - `🌐 Starting FastAPI server on port 8000...`
   - `✅ OutboundAI server started`
   - `🤖 Starting LiveKit agent worker...`
   - `[OK] Supabase connected`  ← if you see `[WARN] Supabase connection failed`, fix the
     Supabase URL/key (usually the key-name mismatch above).

---

## 9. Configure HTTPS + domain (recommended)

1. In Coolify, open the application → **Domains** → add `https://app.yourdomain.com`.
2. Coolify provisions a free Let's Encrypt certificate automatically (its built-in Traefik
   proxy handles routing and TLS).
3. Make sure your domain's DNS A record points to the droplet IP.
4. The dashboard will then be at `https://app.yourdomain.com/ui`.

---

## 10. Verify the deployment

```bash
# health check
curl https://app.yourdomain.com/health
# → {"status":"ok", "livekit_url":"wss://...", "supabase":true}

# stats endpoint (confirms DB read works)
curl https://app.yourdomain.com/stats
```

Then in the browser:
1. Open `https://app.yourdomain.com/ui`.
2. Go to **Settings** — confirm your keys show as `configured`.
3. Go to **Single Call**, enter your own phone number, and click **Dial Now**.
4. Watch Coolify runtime logs: you should see the dispatch, the agent joining the room, and the
   Gemini session starting. Your phone should ring.
5. After the call, check **Call Logs** / **Dashboard** for the logged outcome.

---

## 11. Security hardening (do this before real use)

The API and dashboard have **no built-in authentication** — anyone who reaches the URL can place
calls (which cost money) and read your data. Choose at least one:

1. **Coolify Basic Auth / proxy auth** — put HTTP basic auth in front of the app via Coolify's
   proxy (simplest).
2. **IP allowlist** — restrict the domain to your office/home IP via the proxy or a DO firewall.
3. **Reverse-proxy login** (e.g. Authelia / oauth2-proxy) in front of the container.

Also recommended:
- **DigitalOcean Cloud Firewall**: allow inbound only on 22 (SSH, ideally your IP only), 80, 443.
  Do not expose the raw container port publicly.
- Rotate the Supabase service key and LiveKit secret periodically.
- Tighten CORS in `server.py` (`allow_origins=["*"]`) to your real domain once you have one.

---

## 12. Updating the app

Coolify can auto-deploy on push:
1. App → **Settings** → enable **Auto Deploy** (or configure the Git webhook Coolify provides).
2. Push to your deploy branch → Coolify rebuilds and redeploys.

Manual redeploy: just click **Deploy** again in the Coolify UI.

> Settings you change in the **dashboard** (Settings page) are stored in Supabase and loaded by
> the agent at startup — they survive redeploys. Env-var changes in Coolify require a redeploy
> to take effect.

---

## 13. Troubleshooting

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| `[WARN] Supabase connection failed` in logs | `SUPABASE_SERVICE_ROLE_KEY` missing/misnamed, or schema not run | Set the correctly-named key; run `supabase_schema.sql` |
| `/call/single` returns 500 "OUTBOUND_TRUNK_ID not set" | Trunk id missing | Create trunk (step 5), set `OUTBOUND_TRUNK_ID` |
| Call dispatches but phone never rings | Vobiz creds/trunk wrong, or number format | Verify `VOBIZ_*`; run `setup_trunk.py`; use E.164 (`+91...`) |
| Phone answers but no/garbled audio | Gemini key/model issue, or realtime plugin missing | Check `GOOGLE_API_KEY`/`GEMINI_MODEL`; try `USE_GEMINI_REALTIME=false` to use the pipeline fallback (needs `DEEPGRAM_API_KEY`) |
| Agent never joins the room | `agent_name` mismatch | Both `agent.py` and `server.py` must use `outbound-ai` |
| Build fails on audio libs | base image/deps | The root `Dockerfile` already installs `libgomp1`, `libglib2.0-0`, `libsndfile1`; don't strip them |
| Transfers fail | SIP REFER disabled at Vobiz | Enable Call Transfer in Vobiz; ensure `DEFAULT_TRANSFER_NUMBER` set |
| Container restarts repeatedly | one process crashing | Check runtime logs; `start.sh` waits on both PIDs, so either crashing exits the container |

---

## 14. Quick reference — deploy checklist

- [ ] Droplet created (Ubuntu, ≥2 vCPU / 4 GB), IP noted
- [ ] Coolify installed, admin account created
- [ ] Repo on GitHub/GitLab, `.env` NOT committed
- [ ] Supabase schema run; URL + service_role key copied
- [ ] LiveKit SIP trunk created; `OUTBOUND_TRUNK_ID` copied
- [ ] Coolify app created from repo, Dockerfile build pack, port 8000
- [ ] All required env vars set (esp. `SUPABASE_SERVICE_ROLE_KEY`)
- [ ] Deployed; logs show `✅ OutboundAI server started` + `[OK] Supabase connected`
- [ ] Domain + HTTPS configured
- [ ] `/health` returns ok; test call succeeds
- [ ] Auth / firewall in front of the app before going live
