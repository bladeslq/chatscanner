"""2GIS geocoder cascade — replaces ai/dadata.py.

Pipeline:
  1. Haiku district_hint (caller already extracted)
  2. Haiku district_candidates (multi-district streets)
  3. 2GIS Places by ЖК → reverse-geocode polygon point
  4. 2GIS Geocoder by address (Kazan filter)
  5. 2GIS street_buildings (street without house — sample buildings)
  6. unknown → kept as-is, no fallback "Пригород"

All API responses are cached in-memory + persisted to data/geocoder_cache.json
to survive restarts.
"""
import asyncio
import json
import logging
import os
from pathlib import Path

import aiohttp

from config import DGIS_API_KEY

logger = logging.getLogger(__name__)

KAZAN_LON = 49.108795
KAZAN_LAT = 55.796289
KAZAN_RADIUS = 15000  # ~15km, covers all 7 city districts incl. Салават Купере

KAZAN_CITY_DISTRICTS = [
    "Авиастроительный", "Вахитовский", "Кировский", "Московский",
    "Ново-Савиновский", "Приволжский", "Советский",
]
KAZAN_SUBURB_DISTRICTS = [
    "Пестречинский", "Лаишевский", "Зеленодольский", "Высокогорский",
]
ALL_DISTRICTS = KAZAN_CITY_DISTRICTS + KAZAN_SUBURB_DISTRICTS


# ── Cache (file-backed) ────────────────────────────────────────────
_CACHE_PATH = Path(__file__).resolve().parent.parent / "data" / "geocoder_cache.json"
_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
try:
    _cache: dict = json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
except Exception:
    _cache = {}
_cache_lock = asyncio.Lock()


async def _save_cache() -> None:
    async with _cache_lock:
        try:
            _CACHE_PATH.write_text(json.dumps(_cache, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            logger.warning(f"Geocoder cache save failed: {e}")


# ── District normalizer ────────────────────────────────────────────
def normalize_district(raw: str | None) -> str | None:
    if not raw:
        return None
    rl = raw.lower().replace("ё", "е").replace("-", "").replace(" ", "")
    for d in ALL_DISTRICTS:
        dl = d.lower().replace("ё", "е").replace("-", "").replace(" ", "")
        if dl in rl or rl in dl:
            return d
    return None


def _extract_district_from_admdiv(adm_div: list) -> str | None:
    for div in adm_div or []:
        if div.get("type") == "district":
            n = normalize_district(div.get("name", ""))
            if n:
                return n
    for div in adm_div or []:
        if div.get("type") == "district_area":
            name = div.get("name", "")
            nl = name.lower()
            if "городской округ" in nl or "казань городской" in nl:
                continue
            n = normalize_district(name)
            if n:
                return n
    return None


# ── 2GIS API ───────────────────────────────────────────────────────
async def _dgis_get(session: aiohttp.ClientSession, url: str, params: dict, timeout: int = 12):
    try:
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=timeout)) as r:
            if r.status != 200:
                logger.warning(f"2GIS HTTP {r.status} for {url}: {(await r.text())[:200]}")
                return None
            return await r.json()
    except Exception as e:
        logger.warning(f"2GIS error {url}: {e}")
        return None


async def _dgis_reverse(session, lat: float, lon: float) -> str | None:
    """Reverse-geocode: find nearest building's district."""
    data = await _dgis_get(session, "https://catalog.api.2gis.com/3.0/items/geocode", {
        "lat": lat, "lon": lon,
        "fields": "items.adm_div,items.full_name",
        "type": "building", "radius": 500,
        "key": DGIS_API_KEY,
    })
    if not data:
        return None
    items = data.get("result", {}).get("items", [])
    if not items:
        return None
    return _extract_district_from_admdiv(items[0].get("adm_div", []))


async def dgis_places(complex_name: str) -> str | None:
    """Find ЖК in 2GIS catalog. If first item is the polygon (type=adm_div) without
    district, reverse-geocode its centroid for the actual building's district.

    Two-stage query: first ask "ЖК <name>" so 2GIS narrows to residential complexes
    (avoids matching e.g. "МЧС" → Ministry of Emergency offices). Fall back to the
    bare name only if the prefixed query returns nothing.
    """
    if not DGIS_API_KEY or not complex_name:
        return None
    # v2: query is now "ЖК <name>" instead of bare name; old cache entries (e.g.
    # "МЧС" → Ministry of Emergency in Vakhitovsky) must not be reused.
    cache_key = f"places::v2::{complex_name.lower().strip()}"
    if cache_key in _cache:
        return _cache[cache_key].get("district")

    async def _query(q: str):
        return await _dgis_get(session, "https://catalog.api.2gis.com/3.0/items", {
            "q": q,
            "location": f"{KAZAN_LON},{KAZAN_LAT}",
            "radius": KAZAN_RADIUS,
            "fields": "items.name,items.adm_div,items.point,items.type",
            "page_size": 5,
            "key": DGIS_API_KEY,
        })

    name = complex_name.strip()
    has_zhk_prefix = name.lower().startswith(("жк ", "жк."))

    async with aiohttp.ClientSession() as session:
        data = await _query(name if has_zhk_prefix else f"ЖК {name}")
        items = (data or {}).get("result", {}).get("items", [])
        # Fallback to bare name if "ЖК <name>" found nothing — covers complexes
        # that 2GIS indexes without the prefix.
        if not items and not has_zhk_prefix:
            data = await _query(name)
            items = (data or {}).get("result", {}).get("items", [])

        if not items:
            _cache[cache_key] = {"district": None}
            await _save_cache()
            return None

        primary = next((it for it in items if it.get("type") == "adm_div" and it.get("point")), None)
        if not primary:
            primary = next((it for it in items if it.get("point")), items[0])

        district = _extract_district_from_admdiv(primary.get("adm_div", []))
        if not district and primary.get("point"):
            pt = primary["point"]
            district = await _dgis_reverse(session, pt["lat"], pt["lon"])

    _cache[cache_key] = {"district": district}
    await _save_cache()
    return district


async def dgis_geocoder(address: str) -> str | None:
    """Geocode address via 2GIS, with Kazan filter to avoid namesake streets in suburbs."""
    if not DGIS_API_KEY or not address:
        return None
    cache_key = f"geocoder::{address.lower().strip()}"
    if cache_key in _cache:
        return _cache[cache_key].get("district")

    async with aiohttp.ClientSession() as session:
        data = await _dgis_get(session, "https://catalog.api.2gis.com/3.0/items/geocode", {
            "q": f"Казань, {address}",
            "fields": "items.adm_div,items.full_name,items.point",
            "type": "building,street,adm_div.place",
            "location": f"{KAZAN_LON},{KAZAN_LAT}",
            "radius": KAZAN_RADIUS,
            "sort_point": f"{KAZAN_LON},{KAZAN_LAT}",
            "key": DGIS_API_KEY,
        })
        if not data:
            return None
        items = data.get("result", {}).get("items", [])

        def in_kazan(it):
            for div in it.get("adm_div", []):
                if div.get("type") == "district_area":
                    n = (div.get("name") or "").lower()
                    if "казань городской" in n:
                        return True
                if div.get("type") == "district":
                    if normalize_district(div.get("name", "")) in KAZAN_CITY_DISTRICTS:
                        return True
            return False

        kazan_items = [it for it in items if in_kazan(it)]
        candidates = kazan_items if kazan_items else items[:1]
        district = None
        if candidates:
            district = _extract_district_from_admdiv(candidates[0].get("adm_div", []))

    _cache[cache_key] = {"district": district}
    await _save_cache()
    return district


_PROBE_HOUSES = (1, 15, 40, 80, 150)


async def dgis_street_districts(street: str) -> tuple[list[str], bool]:
    """Sample buildings at fixed house numbers to find which district(s) the
    street passes through.

    2GIS catalog API returns 0 results for bare street name with type=building,
    so we probe specific house numbers (1, 15, 40, 80, 150) and collect all
    distinct Kazan districts seen. Filter results by full_name starting with
    "Казань" to avoid namesake streets in suburbs.

    Returns (districts_sorted_by_count, low_confidence).
    low_confidence=True if fewer than 2 sample points returned Kazan houses —
    caller flags the listing as multi-district even if a single district was
    returned (insufficient evidence to claim it's truly single).
    """
    if not DGIS_API_KEY or not street:
        return [], False
    # v2: cache invalidated; v1 stored mostly empty results from a broken query.
    cache_key = f"street::v2::{street.lower().strip()}"
    if cache_key in _cache:
        c = _cache[cache_key]
        return c.get("districts", []), c.get("low_confidence", False)

    seen: dict[str, int] = {}
    kazan_count = 0

    async with aiohttp.ClientSession() as session:
        async def _probe(house: int) -> None:
            nonlocal kazan_count
            data = await _dgis_get(session, "https://catalog.api.2gis.com/3.0/items", {
                "q": f"{street} {house}",
                "location": f"{KAZAN_LON},{KAZAN_LAT}",
                "radius": KAZAN_RADIUS,
                "fields": "items.adm_div,items.full_name",
                "type": "building",
                "page_size": 10,
                "key": DGIS_API_KEY,
            })
            if not data:
                return
            for it in data.get("result", {}).get("items", []):
                full_name = it.get("full_name", "")
                if not full_name.startswith("Казань"):
                    continue  # skip namesake streets in suburbs
                kazan_count += 1
                d = _extract_district_from_admdiv(it.get("adm_div", []))
                if d:
                    seen[d] = seen.get(d, 0) + 1

        await asyncio.gather(*(_probe(h) for h in _PROBE_HOUSES))

    ordered = sorted(seen.items(), key=lambda x: -x[1])
    # Threshold: only keep districts that scored >= 70% of the leader's count.
    # Filters out 2GIS data-quality false-positives — some Kazan streets have
    # duplicate building records cross-listed in a neighboring district even
    # though locals consider the street single-district (e.g. Меридианная on
    # Ново-Сав/Приволж boundary, Декабристов on Кировский/Московский boundary).
    if ordered:
        leader_count = ordered[0][1]
        cutoff = leader_count * 0.7
        districts = [d for d, c in ordered if c >= cutoff]
    else:
        districts = []
    # Low confidence: only one sample point or no points at all — caller will
    # widen the listing's districts_all to multi for safety.
    low_confidence = kazan_count < 2

    _cache[cache_key] = {"districts": districts, "low_confidence": low_confidence}
    await _save_cache()
    return districts, low_confidence


# ── Cascade ────────────────────────────────────────────────────────
def _has_house_number(addr: str | None) -> bool:
    if not addr:
        return False
    import re as _re
    return bool(_re.search(r"\d+\s*[а-яa-z]?\s*$|\d+\s*[а-яa-z]?[,/-]", addr.strip().lower()))


async def resolve_district(listing: dict) -> dict:
    """Resolve canonical district for a listing using the optimal cascade.

    Sets these fields on the returned dict:
      district: str | None     — primary district to use for hard filter
      districts_all: list[str] — all candidates (1 if single, 2+ if multi-district)
      district_multi: bool     — true if street/area covers multiple districts
      district_source: str     — which step of the cascade resolved it
    """
    out = {**listing}
    addr = listing.get("address")
    cmplx = listing.get("complex")

    # Step 1: LLM district_hint
    hint = normalize_district(listing.get("district_hint"))
    if hint:
        out.update({
            "district": hint,
            "districts_all": [hint],
            "district_multi": False,
            "district_source": "llm_hint",
        })
        return out

    # Step 2: LLM district_candidates (multi-district streets)
    cand_raw = listing.get("district_candidates") or []
    if isinstance(cand_raw, list) and cand_raw:
        cand_norm = []
        for c in cand_raw:
            n = normalize_district(c)
            if n and n not in cand_norm:
                cand_norm.append(n)
        if cand_norm:
            out.update({
                "district": cand_norm[0],
                "districts_all": cand_norm,
                "district_multi": len(cand_norm) > 1,
                "district_source": "llm_candidates",
            })
            return out

    # Step 3: 2GIS Places by ЖК
    if cmplx:
        d = await dgis_places(cmplx)
        if d:
            out.update({
                "district": d,
                "districts_all": [d],
                "district_multi": False,
                "district_source": "2gis_places",
            })
            return out

    # Step 4: 2GIS Geocoder by address — ONLY when there's a house number.
    # For street-without-house, skip directly to street_buildings (Step 5),
    # which probes multiple buildings and detects multi-district via bbox.
    # Otherwise Geocoder might return a single random house's district and
    # mask the fact that the street crosses several districts.
    if addr and _has_house_number(addr):
        d = await dgis_geocoder(addr)
        if d:
            out.update({
                "district": d,
                "districts_all": [d],
                "district_multi": False,
                "district_source": "2gis_geocoder",
            })
            return out

    # Step 5: 2GIS street_buildings (street without house — sample buildings)
    if addr and not _has_house_number(addr):
        districts, low_conf = await dgis_street_districts(addr)
        if districts:
            multi = len(districts) > 1 or low_conf
            out.update({
                "district": districts[0],
                "districts_all": districts if multi else [districts[0]],
                "district_multi": multi,
                "district_source": "street_buildings",
            })
            return out

    # Unknown
    out.update({
        "district": None,
        "districts_all": [],
        "district_multi": False,
        "district_source": None,
    })
    return out
