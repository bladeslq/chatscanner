from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy import select, update
from typing import Optional, List
from config import DATABASE_URL
from database.models import Base, User, Client, MonitoredChat, Match

engine = create_async_engine(DATABASE_URL, echo=False)
async_session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_or_create_user(telegram_id: int, username: str = None, first_name: str = None) -> User:
    async with async_session() as session:
        result = await session.execute(select(User).where(User.telegram_id == telegram_id))
        user = result.scalar_one_or_none()
        if not user:
            user = User(telegram_id=telegram_id, username=username, first_name=first_name)
            session.add(user)
            await session.commit()
            await session.refresh(user)
        return user


async def get_user(telegram_id: int) -> Optional[User]:
    async with async_session() as session:
        result = await session.execute(select(User).where(User.telegram_id == telegram_id))
        return result.scalar_one_or_none()


async def update_user(telegram_id: int, **kwargs):
    async with async_session() as session:
        await session.execute(update(User).where(User.telegram_id == telegram_id).values(**kwargs))
        await session.commit()


async def set_work_mode(telegram_id: int, is_working: bool):
    await update_user(telegram_id, is_working=is_working)


# --- Clients ---

async def create_client(user_id: int, **kwargs) -> Client:
    async with async_session() as session:
        client = Client(user_id=user_id, **kwargs)
        session.add(client)
        await session.commit()
        await session.refresh(client)
        return client


async def get_clients(user_id: int, active_only: bool = True) -> List[Client]:
    async with async_session() as session:
        q = select(Client).where(Client.user_id == user_id)
        if active_only:
            q = q.where(Client.is_active == True)
        q = q.order_by(Client.created_at.desc())
        result = await session.execute(q)
        return list(result.scalars().all())


async def get_client(client_id: int) -> Optional[Client]:
    async with async_session() as session:
        result = await session.execute(select(Client).where(Client.id == client_id))
        return result.scalar_one_or_none()


async def update_client(client_id: int, **kwargs):
    async with async_session() as session:
        await session.execute(update(Client).where(Client.id == client_id).values(**kwargs))
        await session.commit()


async def delete_client(client_id: int):
    await update_client(client_id, is_active=False)


# --- Monitored Chats ---

async def get_monitored_chats(user_id: int, active_only: bool = True) -> List[MonitoredChat]:
    async with async_session() as session:
        q = select(MonitoredChat).where(MonitoredChat.user_id == user_id)
        if active_only:
            q = q.where(MonitoredChat.is_active == True)
        result = await session.execute(q)
        return list(result.scalars().all())


async def add_monitored_chat(user_id: int, chat_id: int, chat_name: str, chat_username: str = None) -> MonitoredChat:
    async with async_session() as session:
        # check if already exists
        result = await session.execute(
            select(MonitoredChat).where(MonitoredChat.user_id == user_id, MonitoredChat.chat_id == chat_id)
        )
        existing = result.scalar_one_or_none()
        if existing:
            existing.is_active = True
            existing.chat_name = chat_name
            await session.commit()
            return existing
        chat = MonitoredChat(user_id=user_id, chat_id=chat_id, chat_name=chat_name, chat_username=chat_username)
        session.add(chat)
        await session.commit()
        await session.refresh(chat)
        return chat


async def remove_monitored_chat(user_id: int, chat_id: int):
    async with async_session() as session:
        await session.execute(
            update(MonitoredChat)
            .where(MonitoredChat.user_id == user_id, MonitoredChat.chat_id == chat_id)
            .values(is_active=False)
        )
        await session.commit()


# --- Matches ---

async def get_client_matches(client_id: int, limit: int = 50) -> List[Match]:
    async with async_session() as session:
        q = (select(Match)
             .where(Match.client_id == client_id)
             .order_by(Match.sent_at.desc())
             .limit(limit))
        result = await session.execute(q)
        return list(result.scalars().all())


async def save_match(user_id: int, client_id: int, chat_id: int, chat_name: str,
                     message_id: int, message_text: str, extracted_data: dict, match_score: int) -> Match:
    async with async_session() as session:
        match = Match(
            user_id=user_id, client_id=client_id, chat_id=chat_id, chat_name=chat_name,
            message_id=message_id, message_text=message_text,
            extracted_data=extracted_data, match_score=match_score,
        )
        session.add(match)
        await session.commit()
        await session.refresh(match)
        return match
