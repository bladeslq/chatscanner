"""
DaData address enrichment.

Two strategies:
 1. If listing has a ЖК name → suggest API (searches by complex name).
 2. If listing has a street address → clean API (standardizes the string).

In both cases we try to extract city_district and map it to one of
the known Kazan districts. The caller keeps Grok's district if DaData
returns nothing.
"""
import logging
import aiohttp
from difflib import SequenceMatcher
from config import DADATA_API_KEY, DADATA_SECRET_KEY, KAZAN_DISTRICTS, KAZAN_COMPLEXES

FUZZY_THRESHOLD = 0.65  # min similarity ratio to accept a match

logger = logging.getLogger(__name__)

_CLEAN_URL = "https://cleaner.dadata.ru/api/v1/clean/address"
_SUGGEST_URL = "https://suggestions.dadata.ru/suggestions/api/4_1/rs/suggest/address"

_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "Authorization": f"Token {DADATA_API_KEY}",
    "X-Secret": DADATA_SECRET_KEY,
}


def _match_district(raw: str | None) -> str | None:
    if not raw:
        return None
    raw_lower = raw.lower()
    for d in KAZAN_DISTRICTS:
        if d.lower() in raw_lower or raw_lower in d.lower():
            return d
    return None


async def _suggest_district(query: str) -> str | None:
    """Use DaData suggest API — good for ЖК names."""
    payload = {
        "query": query,
        "count": 1,
        "locations": [{"city": "Казань"}],
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(_SUGGEST_URL, json=payload, headers=_HEADERS, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                suggestions = data.get("suggestions", [])
                if not suggestions:
                    return None
                return _match_district(suggestions[0].get("data", {}).get("city_district"))
    except Exception as e:
        logger.warning(f"DaData suggest error: {e}")
        return None


async def _clean_district(address: str) -> str | None:
    """Use DaData clean API — good for street addresses."""
    payload = [f"Казань, {address}"]
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(_CLEAN_URL, json=payload, headers=_HEADERS, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                if not data:
                    return None
                return _match_district(data[0].get("city_district"))
    except Exception as e:
        logger.warning(f"DaData clean error: {e}")
        return None


def _lookup_local(complex_name: str) -> str | None:
    """Check local KAZAN_COMPLEXES with substring + fuzzy matching."""
    name_lower = complex_name.lower().strip()
    best_district = None
    best_ratio = 0.0

    for key, district in KAZAN_COMPLEXES.items():
        # Exact substring match — highest priority
        if key in name_lower or name_lower in key:
            return district
        # Fuzzy match for typos / 1-2 wrong letters
        ratio = SequenceMatcher(None, name_lower, key).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_district = district

    if best_ratio >= FUZZY_THRESHOLD:
        logger.debug(f"Fuzzy match '{complex_name}' → '{best_district}' ({best_ratio:.0%})")
        return best_district
    return None


async def enrich_district(listing: dict) -> dict:
    """
    Resolve district with priority:
    1. Local KAZAN_COMPLEXES dict (exact/partial match on ЖК name)
    2. DaData suggest (by ЖК name)
    3. DaData clean (by street address)
    4. Grok's own guess — kept as-is
    """
    complex_name = listing.get("complex")
    address = listing.get("address")
    district = None

    if complex_name:
        district = _lookup_local(complex_name)
        if district:
            logger.info(f"Local dict resolved district by ЖК '{complex_name}': {district}")

    if not district and complex_name:
        district = await _suggest_district(f"ЖК {complex_name} Казань")
        if district:
            logger.info(f"DaData resolved district by ЖК '{complex_name}': {district}")

    if not district and address:
        district = await _clean_district(address)
        if district:
            logger.info(f"DaData resolved district by address '{address}': {district}")

    if district:
        listing = {**listing, "district": district}
    else:
        logger.debug(f"Could not resolve district, keeping Grok's: {listing.get('district')}")

    return listing
