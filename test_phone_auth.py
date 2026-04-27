"""
Standalone CLI test for phone-based Telethon auth.

Run BEFORE wiring this flow into the bot to verify that Telegram does NOT
terminate the session as "untrusted login". If this script logs in and
prints your @username without errors — the device params in
userbot/scanner.py are good enough, and the bot flow will work too.

Usage:
    python test_phone_auth.py

Important: when prompted for the code from Telegram, type it WITH SPACES
or DASHES between digits (e.g. "1 2 3 4 5" or "1-2-3-4-5"). Otherwise
Telegram detects the code as auto-forwarded and kills the session.
"""
import asyncio
import re

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import (
    SessionPasswordNeededError,
    PhoneCodeInvalidError,
    PhoneCodeExpiredError,
    PasswordHashInvalidError,
)

from config import API_ID, API_HASH
from userbot.scanner import DEVICE_PARAMS


def _strip(s: str) -> str:
    return re.sub(r"\D", "", s or "")


async def main() -> None:
    print("=== ChatScanner phone-auth test ===\n")
    print(f"Device params: {DEVICE_PARAMS}\n")

    phone = input("Phone (e.g. +79991234567): ").strip()
    if not phone.startswith("+"):
        phone = "+" + phone

    client = TelegramClient(StringSession(), API_ID, API_HASH, **DEVICE_PARAMS)
    await client.connect()

    print("\nRequesting code...")
    sent = await client.send_code_request(phone)
    print("Code sent. Check Telegram (official app).\n")
    print("⚠️  Type the code WITH SPACES between digits, e.g. '1 2 3 4 5'")
    code_raw = input("Code: ")
    code = _strip(code_raw)

    try:
        await client.sign_in(phone=phone, code=code, phone_code_hash=sent.phone_code_hash)
    except SessionPasswordNeededError:
        password = input("\n2FA password: ").strip()
        try:
            await client.sign_in(password=password)
        except PasswordHashInvalidError:
            print("❌ Wrong 2FA password.")
            await client.disconnect()
            return
    except PhoneCodeInvalidError:
        print("❌ Wrong code.")
        await client.disconnect()
        return
    except PhoneCodeExpiredError:
        print("❌ Code expired. Try again.")
        await client.disconnect()
        return

    me = await client.get_me()
    print(f"\n✅ Logged in: {me.first_name} (@{me.username})")
    print(f"   Phone in profile: {me.phone}")

    # Re-check after a short delay — if Telegram had killed the session as
    # "untrusted", is_user_authorized() would now flip to False.
    await asyncio.sleep(3)
    still_ok = await client.is_user_authorized()
    if still_ok:
        print("✅ Session is still alive 3s later — Telegram did NOT flag as untrusted.")
    else:
        print("❌ Session got terminated. Telegram flagged this login as untrusted.")
        await client.disconnect()
        return

    session_string = client.session.save()
    print(f"\n📋 Session string (for /set_session, optional):\n{session_string}\n")

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
