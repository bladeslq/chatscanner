"""
Fetches residential complexes (ZhK) in Kazan from 2GIS Catalog API.
Queries each district separately to work around the 50-item demo key limit.
Usage: py -3 scrape_2gis.py YOUR_API_KEY
"""
import asyncio
import ssl
import sys
import json
import aiohttp

PAGE_SIZE = 10
ZONE_RADIUS = 5000  # 5 km per zone — small enough for unique results

# Geographic zones covering Kazan (lat, lon, label)
# Each zone hits a different part of the city → different 50-item window
KAZAN_ZONES = [
    # Centre
    (55.796, 49.109, "центр"),
    # Vakhitovsky sub-zones
    (55.810, 49.090, "вахитовский север"),
    (55.785, 49.115, "вахитовский юг"),
    # Novo-Savinovsky
    (55.800, 49.190, "ново-савиновский центр"),
    (55.820, 49.210, "ново-савиновский север"),
    (55.775, 49.205, "ново-савиновский юг"),
    # Sovetsky
    (55.840, 49.155, "советский запад"),
    (55.850, 49.210, "советский восток"),
    (55.865, 49.175, "советский север"),
    # Privolzhsky
    (55.750, 49.195, "приволжский центр"),
    (55.730, 49.170, "приволжский юг"),
    (55.755, 49.235, "приволжский восток"),
    # Moskovsky
    (55.750, 49.090, "московский центр"),
    (55.735, 49.115, "московский юг"),
    # Kirovsky
    (55.775, 49.050, "кировский"),
    (55.800, 49.040, "кировский север"),
    # Aviastroitelny
    (55.850, 49.060, "авиастроительный центр"),
    (55.870, 49.090, "авиастроительный восток"),
]

# Search query variants — run each for every zone
ZONE_QUERIES = ["жилой комплекс", "жк", "апартаменты", "клубный дом"]

DISTRICT_NORMALIZE = {
    "советский район": "Советский",
    "приволжский район": "Приволжский",
    "кировский район": "Кировский",
    "вахитовский район": "Вахитовский",
    "авиастроительный район": "Авиастроительный",
    "ново-савиновский район": "Ново-Савиновский",
    "новосавиновский район": "Ново-Савиновский",
    "московский район": "Московский",
    "советский": "Советский",
    "приволжский": "Приволжский",
    "кировский": "Кировский",
    "вахитовский": "Вахитовский",
    "авиастроительный": "Авиастроительный",
    "ново-савиновский": "Ново-Савиновский",
    "новосавиновский": "Ново-Савиновский",
    "московский": "Московский",
}


def get_district(adm_div: list) -> str | None:
    for div in (adm_div or []):
        if div.get("type") == "district":
            raw = div.get("name", "").lower().strip()
            return DISTRICT_NORMALIZE.get(raw)
    return None


def normalize_name(raw: str) -> str:
    name = raw.strip()
    for suffix in (", жилой комплекс", ", жк", ", апартаменты", ", клубный дом"):
        if name.lower().endswith(suffix):
            name = name[:-len(suffix)]
    for prefix in ("жилой комплекс ", "жк ", "жк«", 'жк "', "апартаменты ", "клубный дом "):
        if name.lower().startswith(prefix):
            name = name[len(prefix):]
    return name.strip().strip("«»\"'").lower()


def process_item(item: dict, complexes: dict):
    name_raw = item.get("name", "").strip()
    if not name_raw:
        return
    district = get_district(item.get("adm_div", []))
    if not district:
        return
    key = normalize_name(name_raw)
    if not key:
        return
    complexes[key] = district


async def fetch_page(session, api_key: str, query: str, lat: float, lon: float, page: int) -> dict:
    params = {
        "q": query,
        "location": f"{lon},{lat}",
        "radius": ZONE_RADIUS,
        "fields": "items.name,items.adm_div",
        "page_size": PAGE_SIZE,
        "page": page,
        "key": api_key,
    }
    async with session.get(
        "https://catalog.api.2gis.com/3.0/items",
        params=params,
        timeout=aiohttp.ClientTimeout(total=15),
    ) as resp:
        if resp.status != 200:
            raise RuntimeError(f"HTTP {resp.status}: {(await resp.text())[:300]}")
        return await resp.json()


async def fetch_zone(session, api_key: str, query: str, lat: float, lon: float, complexes: dict) -> int:
    data = await fetch_page(session, api_key, query, lat, lon, 1)
    result = data.get("result", {})
    total = result.get("total", 0)
    total_pages = min(5, max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE))

    for item in result.get("items", []):
        process_item(item, complexes)

    for page in range(2, total_pages + 1):
        data = await fetch_page(session, api_key, query, lat, lon, page)
        for item in data.get("result", {}).get("items", []):
            process_item(item, complexes)
        await asyncio.sleep(0.2)

    return total


async def main():
    if len(sys.argv) < 2:
        print("Usage: py -3 scrape_2gis.py YOUR_API_KEY")
        return

    api_key = sys.argv[1]
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE

    complexes: dict = {}
    total_combos = len(KAZAN_ZONES) * len(ZONE_QUERIES)
    n = 0

    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ssl_ctx)) as session:
        for lat, lon, label in KAZAN_ZONES:
            for query in ZONE_QUERIES:
                n += 1
                print(f"[{n}/{total_combos}] {label} / {query!r} ...", flush=True)
                try:
                    total = await fetch_zone(session, api_key, query, lat, lon, complexes)
                    print(f"  -> API total: {total}, unique so far: {len(complexes)}", flush=True)
                except Exception as e:
                    print(f"  ERROR: {e}", flush=True)
                await asyncio.sleep(0.3)

    print(f"\nTotal unique ZhK collected: {len(complexes)}")

    with open("zhk_kazan.json", "w", encoding="utf-8") as f:
        json.dump(complexes, f, ensure_ascii=False, indent=2)

    with open("zhk_config_block.txt", "w", encoding="utf-8") as f:
        f.write("KAZAN_COMPLEXES: dict[str, str] = {\n")
        for name, district in sorted(complexes.items()):
            escaped = name.replace('"', '\\"')
            f.write(f'    "{escaped}": "{district}",\n')
        f.write("}\n")

    print("Saved: zhk_kazan.json + zhk_config_block.txt")


if __name__ == "__main__":
    asyncio.run(main())
