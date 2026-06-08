"""
setup_inbound_trunk.py — Provision the inbound voice channel on LiveKit
(e.g. for the phone number you list on your Google My Business profile).

This is the ONE-TIME LiveKit-side setup for inbound calling. It creates:

  1. An inbound SIP trunk that accepts calls for your business number(s).
  2. A SIP dispatch rule that, on every inbound call, spins up a fresh room
     and dispatches the `outbound-ai` agent worker — stamping the
     participant metadata with `call_direction=inbound` and a `source` tag.
     That metadata is what tells agent.py to load the front-desk
     qualification persona (prompts.INBOUND_SYSTEM_PROMPT) instead of the
     outbound booking script. See agent._detect_call_direction().

PRE-REQUISITE (do this first — see INBOUND_SETUP.md):
  Your SIP trunk provider (e.g. Vobiz) must already be forwarding calls for
  your business number to LiveKit's SIP URI. This script only configures the
  LiveKit side — it cannot purchase numbers or change provider routing.

Safe to re-run: it looks for an existing trunk covering the same number /
an existing dispatch rule on that trunk before creating new ones.

Usage:
    python setup_inbound_trunk.py --number "+14165551234"
    python setup_inbound_trunk.py --number "+14165551234" --name "Storefront Line" --source "google_my_business"
"""

import argparse
import asyncio
import json
import os
import sys

# Windows terminals default to cp1252, which can't encode the arrow/dash glyphs
# we print below; force UTF-8 so this never crashes mid-provisioning on Windows.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from dotenv import load_dotenv

load_dotenv(".env")

import livekit.api as lkapi

AGENT_NAME = "outbound-ai"  # MUST match agent.py's WorkerOptions.agent_name


async def main() -> None:
    parser = argparse.ArgumentParser(description="Provision a LiveKit inbound SIP trunk + dispatch rule for inbound calling")
    parser.add_argument(
        "--number", required=True,
        help="The phone number callers will dial, in E.164 format (e.g. +14165551234). "
             "This is the number you'll put on your Google My Business listing.",
    )
    parser.add_argument("--name", default="Inbound Line", help="Display name for the trunk/rule inside LiveKit")
    parser.add_argument(
        "--source", default="google_my_business",
        help="Tag stored on every captured lead from this number, so you can tell "
             "channels apart later if you add more inbound numbers (default: google_my_business)",
    )
    args = parser.parse_args()

    lk_url = os.getenv("LIVEKIT_URL", "")
    lk_key = os.getenv("LIVEKIT_API_KEY", "")
    lk_secret = os.getenv("LIVEKIT_API_SECRET", "")
    if not all([lk_url, lk_key, lk_secret]):
        raise SystemExit("LIVEKIT_URL / LIVEKIT_API_KEY / LIVEKIT_API_SECRET must be set in .env first.")

    lk = lkapi.LiveKitAPI(url=lk_url, api_key=lk_key, api_secret=lk_secret)
    try:
        trunk_id = await _ensure_inbound_trunk(lk, args.name, args.number)
        await _ensure_dispatch_rule(lk, args.name, trunk_id, args.source)
    finally:
        await lk.aclose()

    print()
    print("Next steps:")
    print(f"  1. Add INBOUND_TRUNK_ID={trunk_id} to your .env (or the dashboard Settings page).")
    print(f"  2. Place a real call to {args.number} and watch Live Logs in the dashboard —")
    print("     you should see 'direction=inbound' in the participant log line.")
    print(f"  3. Put {args.number} on your Google My Business profile once the test call sounds right.")


async def _ensure_inbound_trunk(lk: "lkapi.LiveKitAPI", name: str, number: str) -> str:
    existing = await lk.sip.list_sip_inbound_trunk(lkapi.ListSIPInboundTrunkRequest())
    for t in existing.items:
        if number in list(t.numbers):
            print(f"[skip]    Inbound trunk already covers {number}: {t.sip_trunk_id} ({t.name})")
            return t.sip_trunk_id

    trunk = await lk.sip.create_sip_inbound_trunk(
        lkapi.CreateSIPInboundTrunkRequest(
            trunk=lkapi.SIPInboundTrunkInfo(
                name=name,
                numbers=[number],
            )
        )
    )
    print(f"[created] Inbound trunk '{name}' ({trunk.sip_trunk_id}) accepting calls for {number}")
    return trunk.sip_trunk_id


async def _ensure_dispatch_rule(lk: "lkapi.LiveKitAPI", name: str, trunk_id: str, source: str) -> None:
    existing = await lk.sip.list_sip_dispatch_rule(lkapi.ListSIPDispatchRuleRequest())
    for r in existing.items:
        if trunk_id in list(r.trunk_ids):
            print(f"[skip]    Dispatch rule already wired to this trunk: {r.sip_dispatch_rule_id} ({r.name})")
            return

    # Static metadata stamped onto the SIP participant for EVERY call that
    # lands through this trunk. agent.py reads it via
    # _parse_participant_metadata()/_detect_call_direction() to pick the
    # inbound persona and tag captured leads with their channel.
    metadata = json.dumps({"call_direction": "inbound", "source": source})

    rule = await lk.sip.create_sip_dispatch_rule(
        lkapi.CreateSIPDispatchRuleRequest(
            name=f"{name} — Dispatch Rule",
            trunk_ids=[trunk_id],
            rule=lkapi.SIPDispatchRule(
                # One fresh room per inbound call (room names get a random suffix).
                dispatch_rule_individual=lkapi.SIPDispatchRuleIndividual(room_prefix="inbound-")
            ),
            room_config=lkapi.RoomConfiguration(
                agents=[lkapi.RoomAgentDispatch(agent_name=AGENT_NAME, metadata=metadata)],
            ),
        )
    )
    print(f"[created] Dispatch rule ({rule.sip_dispatch_rule_id}): inbound-* rooms → agent '{AGENT_NAME}'")
    print(f"          participant metadata on every call: {metadata}")


if __name__ == "__main__":
    asyncio.run(main())
