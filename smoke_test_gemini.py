"""Gemini API smoke test — verifies an API key works for both REST and Live API.

Usage:
    python smoke_test_gemini.py YOUR_API_KEY
    # or rely on GOOGLE_API_KEY env var:
    python smoke_test_gemini.py
"""
import asyncio
import os
import sys


def get_key() -> str:
    if len(sys.argv) > 1 and sys.argv[1].strip():
        return sys.argv[1].strip()
    return os.getenv("GOOGLE_API_KEY", "").strip()


def key_shape(key: str) -> None:
    print(f"  key length : {len(key)}")
    print(f"  key prefix : {key[:6]}...")
    if key.startswith("AIza"):
        print("  -> Looks like a standard AI Studio API key (good for long-lived use).")
    elif key.startswith("AQ."):
        print("  -> Looks like an EPHEMERAL Live token (short-lived, expires in ~30 min).")
    else:
        print("  -> Unrecognized key format.")


def test_rest(key: str) -> bool:
    print("\n[1/3] REST: list available models ...")
    try:
        from google import genai
        client = genai.Client(api_key=key)
        models = list(client.models.list())
        print(f"  OK — {len(models)} models visible.")
        live = [m.name for m in models if "live" in m.name.lower() or "native-audio" in m.name.lower()]
        if live:
            print("  Live-capable models:")
            for n in live[:10]:
                print(f"    - {n}")
        return True
    except Exception as exc:
        print(f"  FAILED — {type(exc).__name__}: {exc}")
        return False


def test_generate(key: str) -> bool:
    print("\n[2/3] REST: generate a one-line reply ...")
    # Try current text models in order; gemini-2.0-flash is retired for new users.
    candidates = [
        os.getenv("MEMORY_COMPRESSION_MODEL", "gemini-2.5-flash"),
        "gemini-flash-latest",
        "gemini-2.5-flash-lite",
    ]
    from google import genai
    client = genai.Client(api_key=key)
    last_err = None
    for model in candidates:
        try:
            resp = client.models.generate_content(
                model=model,
                contents="Reply with exactly: SMOKE_OK",
            )
            text = (resp.text or "").strip()
            print(f"  model {model} said: {text!r}")
            if "SMOKE_OK" in text:
                return True
        except Exception as exc:
            last_err = exc
            print(f"  model {model} failed: {type(exc).__name__}: {str(exc)[:120]}")
    if last_err:
        print(f"  All candidates failed. Last error: {last_err}")
    return False


async def test_live(key: str) -> bool:
    print("\n[3/3] LIVE: open a realtime bidi session ...")
    model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash-native-audio-latest")
    print(f"  model: {model}")
    try:
        from google import genai
        from google.genai import types
        client = genai.Client(api_key=key)
        # native-audio models only support AUDIO modality; flash-live supports TEXT.
        modality = "AUDIO" if "native-audio" in model else "TEXT"
        print(f"  response modality: {modality}")
        config = types.LiveConnectConfig(response_modalities=[modality])
        async with client.aio.live.connect(model=model, config=config) as session:
            await session.send_client_content(
                turns=types.Content(role="user", parts=[types.Part(text="Say hi in one word.")]),
                turn_complete=True,
            )
            got_audio = False
            chunks = []
            async for msg in session.receive():
                if msg.text:
                    chunks.append(msg.text)
                if msg.data:
                    got_audio = True
                if msg.server_content and msg.server_content.turn_complete:
                    break
            if got_audio:
                print("  Live model returned audio bytes.")
            reply = "".join(chunks).strip()
            if reply:
                print(f"  Live model said: {reply!r}")
            print("  OK — Live API connection works (session opened, response received).")
            return True
    except Exception as exc:
        print(f"  FAILED — {type(exc).__name__}: {exc}")
        return False


async def main() -> None:
    key = get_key()
    print("=" * 60)
    print("GEMINI API SMOKE TEST")
    print("=" * 60)
    if not key:
        print("No API key provided. Pass it as an argument or set GOOGLE_API_KEY.")
        sys.exit(2)
    key_shape(key)

    r1 = test_rest(key)
    r2 = test_generate(key)
    r3 = await test_live(key)

    print("\n" + "=" * 60)
    print("RESULTS")
    print("=" * 60)
    print(f"  REST list models    : {'PASS' if r1 else 'FAIL'}")
    print(f"  REST generate       : {'PASS' if r2 else 'FAIL'}")
    print(f"  LIVE realtime bidi  : {'PASS' if r3 else 'FAIL'}")
    if r1 and r2 and r3:
        print("\nAll checks passed — the key works for both REST and Live API.")
    elif r1 or r2:
        print("\nKey works for REST but NOT Live. The key/model lacks Live API access,")
        print("or (if it starts with AQ.) it is an expired ephemeral token.")
    else:
        print("\nKey failed all checks — it is invalid, revoked, or expired.")


if __name__ == "__main__":
    asyncio.run(main())
