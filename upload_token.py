import asyncio
import sys
import db
from dotenv import load_dotenv

load_dotenv(".env")

# Force stdout to utf-8 if needed, or just catch print errors
sys.stdout.reconfigure(encoding='utf-8')

async def main():
    try:
        with open("token.json", "r", encoding="utf-8") as f:
            token = f.read()
            
        db.init_db()
        await db.set_setting("GOOGLE_OAUTH_TOKEN", token)
        print("Token successfully uploaded to Supabase!")
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    asyncio.run(main())
