import asyncio
import logging
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from config import BOT_TOKEN, OWNER_ID
from database.db import init_db, get_user, get_monitored_chats
from userbot.scanner import scanner
from bot.handlers import start, auth, clients, chats, work_mode

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


async def restore_userbot_sessions(bot: Bot):
    """On startup, restore sessions for authorized users and resume monitoring if was active."""
    user = await get_user(OWNER_ID)
    if not user or not user.session_string or not user.is_authorized:
        logger.info("No saved userbot session found")
        return

    logger.info(f"Restoring userbot session for user {OWNER_ID}")
    try:
        tg_client = await scanner.create_client(OWNER_ID, user.session_string)
        await tg_client.connect()

        if not await tg_client.is_user_authorized():
            logger.warning("Saved session is no longer valid")
            return

        me = await tg_client.get_me()
        logger.info(f"Userbot restored: @{me.username}")

        if user.is_working:
            monitored = await get_monitored_chats(user.id)
            chat_ids = [c.chat_id for c in monitored]
            if chat_ids:
                await scanner.start_listening(OWNER_ID, chat_ids)
                logger.info(f"Resumed monitoring {len(chat_ids)} chats")
                await bot.send_message(
                    OWNER_ID,
                    f"✅ <b>Мониторинг восстановлен</b>\n"
                    f"Слежу за {len(chat_ids)} чатами.",
                    parse_mode="HTML",
                )
    except Exception as e:
        logger.error(f"Failed to restore userbot session: {e}")


async def main():
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())

    # Wire bot reference into work_mode handler
    work_mode.set_bot(bot)

    # Wire notification callback into scanner
    scanner.set_notify_callback(work_mode.process_new_message)

    # Register routers
    dp.include_router(start.router)
    dp.include_router(auth.router)
    dp.include_router(clients.router)
    dp.include_router(chats.router)
    dp.include_router(work_mode.router)

    # Init DB
    await init_db()
    logger.info("Database initialized")

    # Restore userbot sessions
    await restore_userbot_sessions(bot)

    logger.info("Starting bot polling...")
    try:
        await dp.start_polling(bot, allowed_updates=["message", "callback_query"])
    finally:
        await scanner.disconnect_all()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
