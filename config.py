import os
from dotenv import load_dotenv

load_dotenv()

# ── Telegram ────────────────────────────────────────────────────────
BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
API_ID: int = int(os.getenv("API_ID", "0"))
API_HASH: str = os.getenv("API_HASH", "")
OWNER_ID: int = int(os.getenv("OWNER_ID", "0"))

# ── LLM (Anthropic Haiku 4.5) ──────────────────────────────────────
ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")
HAIKU_MODEL: str = "claude-haiku-4-5-20251001"

# ── Geocoding (2GIS) ───────────────────────────────────────────────
DGIS_API_KEY: str = os.getenv("DGIS_API_KEY", "")

# ── Storage ────────────────────────────────────────────────────────
DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///chatscanner.db")

# ── DEPRECATED (left in env for safety; not used in current pipeline) ──
GROK_API_KEY: str = os.getenv("GROK_API_KEY", "")
GROK_BASE_URL: str = "https://api.groq.com/openai/v1"
GROK_MODEL: str = "meta-llama/llama-4-scout-17b-16e-instruct"
DADATA_API_KEY: str = os.getenv("DADATA_API_KEY", "")
DADATA_SECRET_KEY: str = os.getenv("DADATA_SECRET_KEY", "")
YANDEX_GEOCODER_KEY: str = os.getenv("YANDEX_GEOCODER_KEY", "")  # not used: free tier doesn't return Kazan districts


# ── Districts ──────────────────────────────────────────────────────
# 7 city districts of Kazan + 4 suburb districts (where realtors actually post).
# No catch-all "Пригород" — every listing resolves to a concrete district or
# stays unknown (and is filtered out for clients with district restrictions).
KAZAN_CITY_DISTRICTS = [
    "Авиастроительный",
    "Вахитовский",
    "Кировский",
    "Московский",
    "Ново-Савиновский",
    "Приволжский",
    "Советский",
]
KAZAN_SUBURB_DISTRICTS = [
    "Пестречинский",      # Куюки, Усады, Царёво, Богородское
    "Лаишевский",         # Малые/Большие Кабаны, Сокуры
    "Зеленодольский",     # Новая Тура, Осиново, Айша
    "Высокогорский",      # Высокая Гора, Шапши
]
KAZAN_DISTRICTS = KAZAN_CITY_DISTRICTS + KAZAN_SUBURB_DISTRICTS


PROPERTY_TYPES = {
    "apartment": "Квартира",
    "house": "Дом/Коттедж",
}
