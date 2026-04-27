import logging
from typing import Optional, Callable
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.types import Channel, Chat
from config import API_ID, API_HASH

logger = logging.getLogger(__name__)

# Mimic an official Telegram Desktop client so that logins from this account
# don't get flagged as "untrusted" and immediately terminated by Telegram.
DEVICE_PARAMS = {
    "device_model": "Desktop",
    "system_version": "Windows 10",
    "app_version": "5.7.1 x64",
    "lang_code": "ru",
    "system_lang_code": "ru",
}


class UserBotScanner:
    def __init__(self):
        self._clients: dict[int, TelegramClient] = {}
        self._handlers: dict[int, Callable] = {}
        # telegram_id -> {"phone": str, "phone_code_hash": str}
        self._pending_auth: dict[int, dict] = {}
        self._notify_callback: Optional[Callable] = None

    def set_notify_callback(self, callback: Callable):
        self._notify_callback = callback

    async def create_client(self, telegram_id: int, session_string: str = None) -> TelegramClient:
        # Disconnect existing client if any
        if telegram_id in self._clients:
            try:
                await self._clients[telegram_id].disconnect()
            except Exception:
                pass

        session = StringSession(session_string) if session_string else StringSession()
        tg_client = TelegramClient(session, API_ID, API_HASH, **DEVICE_PARAMS)
        self._clients[telegram_id] = tg_client
        return tg_client

    async def get_client(self, telegram_id: int) -> Optional[TelegramClient]:
        return self._clients.get(telegram_id)

    async def start_listening(self, telegram_id: int, chat_ids: list[int]):
        tg_client = self._clients.get(telegram_id)
        if not tg_client or not tg_client.is_connected():
            logger.error(f"Cannot start listening: client for {telegram_id} not connected")
            return

        # Remove previous handler for this user to avoid duplicates
        await self.stop_listening(telegram_id)

        async def message_handler(event):
            if not self._notify_callback:
                return
            try:
                chat = await event.get_chat()
                chat_name = (
                    getattr(chat, "title", None)
                    or getattr(chat, "username", None)
                    or str(chat.id)
                )
                await self._notify_callback(telegram_id, event.chat_id, chat_name, event)
            except Exception as e:
                logger.error(f"Handler error for user {telegram_id}: {e}")

        tg_client.add_event_handler(message_handler, events.NewMessage(chats=chat_ids))
        self._handlers[telegram_id] = message_handler
        logger.info(f"[UserBot {telegram_id}] Listening to {len(chat_ids)} chats")

    async def stop_listening(self, telegram_id: int):
        tg_client = self._clients.get(telegram_id)
        handler = self._handlers.pop(telegram_id, None)
        if tg_client and handler:
            tg_client.remove_event_handler(handler)
            logger.info(f"[UserBot {telegram_id}] Stopped listening")

    async def get_dialogs(self, telegram_id: int) -> list[dict]:
        tg_client = self._clients.get(telegram_id)
        if not tg_client:
            return []
        dialogs = []
        async for dialog in tg_client.iter_dialogs():
            entity = dialog.entity
            if isinstance(entity, (Channel, Chat)):
                dialogs.append({
                    "id": dialog.id,
                    "name": dialog.name,
                    "username": getattr(entity, "username", None),
                    "members_count": getattr(entity, "participants_count", None),
                })
        return dialogs

    async def save_session(self, telegram_id: int) -> str:
        tg_client = self._clients.get(telegram_id)
        if not tg_client:
            return ""
        return tg_client.session.save()

    async def disconnect_all(self):
        for tg_client in self._clients.values():
            try:
                await tg_client.disconnect()
            except Exception:
                pass


scanner = UserBotScanner()
