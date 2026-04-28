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
from config import DADATA_API_KEY, DADATA_SECRET_KEY, KAZAN_DISTRICTS, KAZAN_COMPLEXES, KAZAN_STREETS

FUZZY_THRESHOLD = 0.65  # min similarity ratio to accept a match

_CYR_TO_LAT = {
    'а': 'a', 'б': 'b', 'в': 'v', 'г': 'g', 'д': 'd',
    'е': 'e', 'ё': 'e', 'ж': 'zh', 'з': 'z', 'и': 'i',
    'й': 'y', 'к': 'k', 'л': 'l', 'м': 'm', 'н': 'n',
    'о': 'o', 'п': 'p', 'р': 'r', 'с': 's', 'т': 't',
    'у': 'u', 'ф': 'f', 'х': 'h', 'ц': 'ts', 'ч': 'ch',
    'ш': 'sh', 'щ': 'sch', 'ъ': '', 'ы': 'y', 'ь': '',
    'э': 'e', 'ю': 'yu', 'я': 'ya',
}


def _to_latin(text: str) -> str:
    """Transliterate Cyrillic → Latin so mixed-script names can be compared."""
    return ''.join(_CYR_TO_LAT.get(c, c) for c in text.lower())


_COMPLEXES_LATIN: dict[str, str] = {_to_latin(k): v for k, v in KAZAN_COMPLEXES.items()}

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
    if not DADATA_API_KEY:
        logger.warning("DADATA_API_KEY is empty — skipping suggest call")
        return None
    payload = {
        "query": query,
        "count": 1,
        "locations": [{"city": "Казань"}],
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(_SUGGEST_URL, json=payload, headers=_HEADERS, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.warning(f"DaData suggest HTTP {resp.status}: {body[:200]}")
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
    if not DADATA_API_KEY:
        logger.warning("DADATA_API_KEY is empty — skipping clean call")
        return None
    payload = [f"Казань, {address}"]
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(_CLEAN_URL, json=payload, headers=_HEADERS, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.warning(f"DaData clean HTTP {resp.status}: {body[:200]}")
                    return None
                data = await resp.json()
                if not data:
                    logger.info(f"DaData clean returned empty list for '{address}'")
                    return None
                raw = data[0].get("city_district")
                matched = _match_district(raw)
                if not matched:
                    logger.info(f"DaData clean got city_district={raw!r}, no match in KAZAN_DISTRICTS")
                return matched
    except Exception as e:
        logger.warning(f"DaData clean error: {e}")
        return None


def _lookup_street(address: str) -> str | None:
    """Match address against the local KAZAN_STREETS dictionary.
    Picks the longest matching key — so 'академика сахарова' beats 'сахарова'.
    """
    addr = address.lower()
    best_key = None
    best_len = 0
    for key in KAZAN_STREETS:
        if key in addr and len(key) > best_len:
            best_key = key
            best_len = len(key)
    return KAZAN_STREETS[best_key] if best_key else None


def _lookup_local(complex_name: str) -> str | None:
    """Check local KAZAN_COMPLEXES with substring + fuzzy matching.
    Both query and keys are transliterated to Latin so Cyrillic/Latin
    variants (e.g. 'арт сити' vs 'art city') match each other.
    """
    name_latin = _to_latin(complex_name.strip())

    # Strip leading "жк " / "жилой комплекс " prefixes before matching
    for prefix in ("zhiloy kompleks ", "zhk "):
        if name_latin.startswith(prefix):
            name_latin = name_latin[len(prefix):]
            break

    # Very short query (≤3 chars): only allow exact match (catches «UNO», «ЖК IQ»)
    if len(name_latin) <= 3:
        return _COMPLEXES_LATIN.get(name_latin)

    best_district = None
    best_ratio = 0.0
    best_substring: tuple[int, str] | None = None  # (key_len, district)

    for key_latin, district in _COMPLEXES_LATIN.items():
        # query is contained in the key: require query covers ≥40% of key
        if name_latin in key_latin and len(name_latin) / len(key_latin) >= 0.4:
            if best_substring is None or len(key_latin) < best_substring[0]:
                best_substring = (len(key_latin), district)
            continue
        # key is contained in query: skip very short keys (≤4 chars) to
        # prevent «uno» matching inside «runo», «азия» inside «евразия»
        if key_latin in name_latin and len(key_latin) >= 5:
            if best_substring is None or len(key_latin) > best_substring[0]:
                best_substring = (len(key_latin), district)
            continue
        # Fuzzy match for typos / transliteration gaps
        ratio = SequenceMatcher(None, name_latin, key_latin).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_district = district

    if best_substring is not None:
        return best_substring[1]

    if best_ratio >= FUZZY_THRESHOLD:
        logger.debug(f"Fuzzy match '{complex_name}' → '{best_district}' ({best_ratio:.0%})")
        return best_district
    return None


async def enrich_district(listing: dict) -> dict:
    """
    Resolve district with priority:
    1. Local KAZAN_COMPLEXES dict (by ЖК name)
    2. DaData suggest API (by ЖК name)
    3. Local KAZAN_STREETS dict (by street address) — fast & doesn't need DaData
    4. DaData clean API (by street address)
    5. Grok's own guess — kept as-is
    6. Fallback: "Пригород"
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
        district = _lookup_street(address)
        if district:
            logger.info(f"Local dict resolved district by street '{address}': {district}")

    if not district and address:
        district = await _clean_district(address)
        if district:
            logger.info(f"DaData resolved district by address '{address}': {district}")
        else:
            logger.info(f"DaData clean returned no district for address '{address}'")

    if not district:
        grok_match = _match_district(listing.get("district"))
        if grok_match:
            district = grok_match
            logger.info(f"Grok district guess accepted: {district}")

    if not district:
        district = "Пригород"
        logger.info(
            f"District not resolved → Пригород "
            f"(complex={complex_name!r}, address={address!r}, grok_guess={listing.get('district')!r})"
        )

    listing = {**listing, "district": district}
    return listing
