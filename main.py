import asyncio
import logging
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, MenuButtonCommands

from config import BOT_TOKEN
from database.db import init_db, get_all_authorized_users, get_monitored_chats
from userbot.scanner import scanner
from bot.handlers import start, auth, clients, chats, work_mode

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


async def restore_userbot_sessions(bot: Bot):
    """On startup, restore sessions for all authorized users."""
    users = await get_all_authorized_users()
    if not users:
        logger.info("No saved userbot sessions found")
        return

    for user in users:
        logger.info(f"Restoring userbot session for user {user.telegram_id}")
        try:
            tg_client = await scanner.create_client(user.telegram_id, user.session_string)
            await tg_client.connect()

            if not await tg_client.is_user_authorized():
                logger.warning(f"Session for {user.telegram_id} is no longer valid")
                continue

            me = await tg_client.get_me()
            logger.info(f"Userbot restored: @{me.username} ({user.telegram_id})")

            if user.is_working:
                monitored = await get_monitored_chats(user.id)
                chat_ids = [c.chat_id for c in monitored]
                if chat_ids:
                    await scanner.start_listening(user.telegram_id, chat_ids)
                    logger.info(f"Resumed monitoring {len(chat_ids)} chats for {user.telegram_id}")
        except Exception as e:
            logger.error(f"Failed to restore session for {user.telegram_id}: {e}")


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

    # Set bot commands menu
    await bot.set_my_commands([
        BotCommand(command="start",  description="Главная"),
        BotCommand(command="auth",   description="Подключить аккаунт"),
        BotCommand(command="reauth", description="Переподключить аккаунт"),
    ])
    await bot.set_chat_menu_button(menu_button=MenuButtonCommands())

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
