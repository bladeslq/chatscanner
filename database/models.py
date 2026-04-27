from sqlalchemy import Column, Integer, String, Boolean, Float, DateTime, ForeignKey, JSON, Text
from sqlalchemy.orm import DeclarativeBase, relationship
from datetime import datetime


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    telegram_id = Column(Integer, unique=True, nullable=False)
    username = Column(String(100))
    first_name = Column(String(100))
    is_working = Column(Boolean, default=False)
    session_string = Column(Text)  # Telethon session string
    phone = Column(String(20))
    is_authorized = Column(Boolean, default=False)  # userbot authorized
    created_at = Column(DateTime, default=datetime.utcnow)

    clients = relationship("Client", back_populates="user", cascade="all, delete-orphan")
    monitored_chats = relationship("MonitoredChat", back_populates="user", cascade="all, delete-orphan")
    matches = relationship("Match", back_populates="user", cascade="all, delete-orphan")


class Client(Base):
    __tablename__ = "clients"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    name = Column(String(200), nullable=False)
    phone = Column(String(20))
    transaction_type = Column(String(10))  # sale / rent
    property_type = Column(String(20))     # apartment / house / commercial / land / room
    min_rooms = Column(Integer)
    max_rooms = Column(Integer)
    min_price = Column(Float)
    max_price = Column(Float)
    min_area = Column(Float)
    max_area = Column(Float)
    districts = Column(JSON)  # list of strings
    notes = Column(Text)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="clients")
    matches = relationship("Match", back_populates="client", cascade="all, delete-orphan")

    def requirements_text(self) -> str:
        from config import PROPERTY_TYPES, TRANSACTION_TYPES
        parts = []
        if self.transaction_type:
            parts.append(TRANSACTION_TYPES.get(self.transaction_type, self.transaction_type))
        if self.min_rooms or self.max_rooms:
            if self.min_rooms == self.max_rooms:
                parts.append(f"{self.min_rooms}-комн.")
            elif self.min_rooms and self.max_rooms:
                parts.append(f"{self.min_rooms}-{self.max_rooms}-комн.")
            elif self.min_rooms:
                parts.append(f"от {self.min_rooms} комн.")
            else:
                parts.append(f"до {self.max_rooms} комн.")
        if self.min_price or self.max_price:
            if self.min_price and self.max_price:
                parts.append(f"{self._fmt_price(self.min_price)}–{self._fmt_price(self.max_price)} ₽")
            elif self.min_price:
                parts.append(f"от {self._fmt_price(self.min_price)} ₽")
            else:
                parts.append(f"до {self._fmt_price(self.max_price)} ₽")
        if self.min_area or self.max_area:
            if self.min_area and self.max_area:
                parts.append(f"{self.min_area}–{self.max_area} м²")
            elif self.min_area:
                parts.append(f"от {self.min_area} м²")
            else:
                parts.append(f"до {self.max_area} м²")
        return " | ".join(parts) if parts else "Без ограничений"

    @staticmethod
    def _fmt_price(price: float) -> str:
        if price >= 1_000_000:
            return f"{price / 1_000_000:.1f}М".rstrip("0").rstrip(".")
        return f"{int(price):,}".replace(",", " ")


class MonitoredChat(Base):
    __tablename__ = "monitored_chats"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    chat_id = Column(Integer, nullable=False)
    chat_name = Column(String(300))
    chat_username = Column(String(100))
    is_active = Column(Boolean, default=True)
    added_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="monitored_chats")


class Match(Base):
    __tablename__ = "matches"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    client_id = Column(Integer, ForeignKey("clients.id"), nullable=False)
    chat_id = Column(Integer)
    chat_name = Column(String(300))
    message_id = Column(Integer)
    message_text = Column(Text)
    extracted_data = Column(JSON)
    match_score = Column(Integer)
    sent_at = Column(DateTime, default=datetime.utcnow)
    # deduplication
    message_hash = Column(String(64))        # MD5 of raw text — exact repost across chats
    listing_fingerprint = Column(String(64)) # MD5 of key listing fields — same flat, different wording

    user = relationship("User", back_populates="matches")
    client = relationship("Client", back_populates="matches")
