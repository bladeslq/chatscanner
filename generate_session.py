"""
Run this script once to generate your Telethon session string.
Then paste the result into the bot via /set_session command.
"""
import asyncio
from telethon import TelegramClient
from telethon.sessions import StringSession
from config import API_ID, API_HASH


async def main():
    print("=== Генерация сессии Telethon ===\n")
    async with TelegramClient(StringSession(), API_ID, API_HASH) as client:
        await client.start()
        session_string = client.session.save()
        me = await client.get_me()
        print(f"\n✅ Аккаунт: {me.first_name} (@{me.username})")
        print(f"\n📋 Твоя сессия (скопируй всё):\n")
        print(session_string)
        print(f"\nТеперь отправь боту команду:\n/set_session {session_string}")


asyncio.run(main())
