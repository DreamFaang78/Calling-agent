# Inbound Calling Setup — "Put this number on Google My Business"

This is the step-by-step guide for turning OutboundAI into an **inbound lead-qualification
line**: someone finds your business on Google, taps "Call", and the AI agent answers,
has a natural discovery conversation, qualifies what they need, books them in if they're
ready — and either way saves a structured lead record + texts you a heads-up.

It reuses the exact same worker (`agent.py`, `agent_name="outbound-ai"`) — no second
deployment needed. The only new pieces are: an **inbound SIP trunk + dispatch rule** in
LiveKit (so calls land in a room automatically), and code that already exists after this
change (the `INBOUND_SYSTEM_PROMPT` persona, the `capture_lead` tool, and the `leads` table).

> Outbound calling keeps working exactly as before — inbound is additive.

---

## 0. What you'll end up with

```
Caller dials your GMB number
        │
        ▼
Your SIP provider (Vobiz) forwards the call to LiveKit's SIP URI
        │
        ▼
LiveKit inbound trunk + dispatch rule  →  creates a room  →  dispatches "outbound-ai"
        │                                    (stamps metadata: call_direction=inbound,
        │                                     source=google_my_business)
        ▼
agent.py detects "inbound"  →  loads INBOUND_SYSTEM_PROMPT (front-desk persona)
        │
        ▼
Natural conversation → capture_lead() saves to `leads` table → SMS alert to you
                     → (if ready) check_availability/book_appointment as usual
```

---

## 1. Get a phone number that can route through LiveKit

You need a number that:
- can be listed publicly on your **Google Business Profile**, AND
- can be configured to **forward/trunk its inbound calls to LiveKit's SIP URI**.

Two common paths:
- **Easiest:** Buy a new DID directly from Vobiz (or your existing SIP provider) and list
  *that* number on Google My Business. No porting required — you control the routing
  from day one.
- **If you must keep your existing GMB number:** you'd need to port it to a
  SIP-capable provider, or set up call forwarding from your current carrier to a Vobiz
  number. Porting/forwarding is provider-specific — check with Vobiz support for your region.

Either way, write the number down in **E.164 format**, e.g. `+14165551234`.

## 2. Find your LiveKit SIP URI

In the LiveKit Cloud dashboard → **Settings → SIP**, copy your project's SIP URI
(looks like `sip:<something>.sip.livekit.cloud` or a similar `sip:` address).
You'll hand this to Vobiz so they know where to forward calls.

## 3. Configure inbound forwarding at Vobiz

In your Vobiz dashboard:
1. Open the number from Step 1.
2. Set its **inbound routing / termination destination** to the LiveKit SIP URI from Step 2.
3. If Vobiz asks for inbound auth (IP allowlist vs. username/password), use whichever
   your LiveKit project's inbound trunk will be configured with — for the simplest
   setup, IP-based trunking (no credentials) is fine; the script in Step 4 creates a
   trunk scoped to your number without requiring inbound auth.

(This part happens entirely in Vobiz's UI — there's no code for it. If you get stuck,
Vobiz support can usually walk you through "forward this DID to a SIP URI.")

## 4. Provision the LiveKit side — run the setup script

This repo now ships `setup_inbound_trunk.py`, which creates:
- an **inbound SIP trunk** scoped to your number, and
- a **SIP dispatch rule** that spins up a room per call and dispatches the
  `outbound-ai` agent, stamping every inbound participant's metadata with
  `{"call_direction": "inbound", "source": "google_my_business"}`.

That metadata is what tells `agent.py` to use the inbound front-desk persona
(`prompts.INBOUND_SYSTEM_PROMPT`) instead of the outbound booking script — see
`agent._detect_call_direction()`.

```bash
# Make sure LIVEKIT_URL / LIVEKIT_API_KEY / LIVEKIT_API_SECRET are in your .env, then:
python setup_inbound_trunk.py --number "+14165551234" --name "Storefront Line"
```

It prints the new `sip_trunk_id` (looks like `ST_xxxxxxxx`) and is safe to re-run —
it detects an existing trunk/rule for the same number and skips creating duplicates.

## 5. Set the new environment variables

Add these to your `.env` (or the dashboard's **Settings** page — both work, settings
override env without a redeploy):

```bash
INBOUND_TRUNK_ID=ST_xxxxxxxx          # printed by setup_inbound_trunk.py
LEAD_ALERT_PHONE_NUMBER=+14165550000  # YOUR phone — gets a text the moment a lead is captured
BUSINESS_NAME="Your Business Name"    # used in the inbound greeting & lead alerts
SERVICE_TYPE="your main service"      # e.g. "dental cleanings", "HVAC repairs"
```

`LEAD_ALERT_PHONE_NUMBER` reuses the **same Twilio credentials** (`TWILIO_ACCOUNT_SID`,
`TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER`) you may already have configured for
booking-confirmation SMS — if those aren't set, lead alerts are silently skipped (the
lead is still saved; you just won't get a text).

## 6. Test it before going live

1. Call your new number from your own phone.
2. Watch **Live Logs** in the dashboard — you should see a line like:
   `Participant: sip:+1416..., phone=+1416..., lead=None, direction=inbound`
3. Have a real conversation — notice the agent answers as "Thanks for calling
   {business_name}, this is Priya..." (NOT "Hi, am I speaking with...").
4. After hanging up, check the new **Leads** page in the dashboard — your test call
   should show up with whatever you told the agent (requirement/urgency/budget).
5. If `LEAD_ALERT_PHONE_NUMBER` + Twilio are configured, you should also get a text.

If `direction=outbound` shows up instead, the dispatch rule's metadata isn't reaching
the agent — re-run `setup_inbound_trunk.py` and confirm it printed "[created]" (not
an error) for the dispatch rule step.

## 7. Go live: add the number to Google My Business

Once a few test calls sound right:
1. Go to your **Google Business Profile** → Edit profile → Contact → Phone number.
2. Enter the number from Step 1.
3. Save. Google may take a short while to propagate the change.

That's it — calls from your Google listing will now be answered, qualified, and
logged automatically.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Phone rings but agent never joins | Dispatch rule not created / wrong `agent_name` | Re-run `setup_inbound_trunk.py`; confirm it matches `agent_name="outbound-ai"` in `agent.py` |
| Agent joins but uses the outbound booking script ("Hi, am I speaking with...") | `call_direction` metadata missing/not detected | Check Live Logs for `direction=...`; re-run the setup script — it stamps `call_direction=inbound` on the dispatch rule |
| No lead shows up on the Leads page after a call | Agent never called `capture_lead`, or DB write failed | Check Live Logs / error_logs for `capture_lead` errors; confirm `supabase_schema.sql` has been (re-)run so the `leads` table exists |
| No SMS alert | `LEAD_ALERT_PHONE_NUMBER` or `TWILIO_*` not set | Set all four (`LEAD_ALERT_PHONE_NUMBER`, `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER`) — alerts fail silently by design so a missing text never breaks the call |
| Call never reaches LiveKit at all | Vobiz forwarding misconfigured | Re-check Step 3 — the destination must be your LiveKit project's SIP URI exactly |

## Re-running / changing numbers later

Adding a second inbound number (e.g. a different city/location)? Just run the script
again with a different `--number` and `--source` (e.g. `--source "yelp"` or
`--source "storefront_sign"`) — leads get tagged by source so you can tell channels
apart on the Leads page.
