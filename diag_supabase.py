"""Diagnostic: read the Supabase settings table + row counts via REST.

These settings OVERRIDE env vars at agent startup (see agent.py
load_db_settings_to_env), so this is the real source of truth for the
deployed agent's model/key. Secrets are masked in output.
"""
import os
import httpx
from dotenv import load_dotenv

load_dotenv(".env")

URL = os.getenv("SUPABASE_URL", "").rstrip("/")
KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "") or os.getenv("SUPABASE_SERVICE_KEY", "")

SENSITIVE = {"GOOGLE_API_KEY", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET",
             "VOBIZ_PASSWORD", "SUPABASE_SERVICE_ROLE_KEY", "DEEPGRAM_API_KEY"}


def mask(k: str, v: str) -> str:
    if k in SENSITIVE and v:
        return f"{v[:8]}…(len {len(v)})"
    return v


def headers():
    return {"apikey": KEY, "Authorization": f"Bearer {KEY}"}


def main():
    print("=" * 60)
    print("SUPABASE DIAGNOSTIC")
    print("=" * 60)
    if not URL or not KEY:
        print("Missing SUPABASE_URL or service role key in .env")
        return
    print(f"URL: {URL}")

    with httpx.Client(timeout=20) as c:
        # 1) settings table — the override source
        print("\n[settings table] (overrides env at agent startup)")
        try:
            r = c.get(f"{URL}/rest/v1/settings?select=key,value", headers=headers())
            if r.status_code != 200:
                print(f"  HTTP {r.status_code}: {r.text[:200]}")
            else:
                rows = r.json()
                if not rows:
                    print("  (empty — agent uses Coolify env vars only)")
                for row in rows:
                    k = row.get("key", "")
                    print(f"  {k} = {mask(k, row.get('value',''))}")
        except Exception as exc:
            print(f"  ERROR: {type(exc).__name__}: {exc}")

        # 2) row counts for the dashboard tables
        for table in ("call_logs", "appointments", "error_logs"):
            try:
                r = c.get(
                    f"{URL}/rest/v1/{table}?select=id",
                    headers={**headers(), "Prefer": "count=exact", "Range": "0-0"},
                )
                cr = r.headers.get("content-range", "?")
                print(f"\n[{table}] HTTP {r.status_code}, content-range: {cr}")
            except Exception as exc:
                print(f"\n[{table}] ERROR: {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    main()
