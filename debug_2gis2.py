"""Debug: print all unique district names seen in adm_div across all pages."""
import asyncio
import ssl
import sys
import json
import aiohttp

PAGE_SIZE = 10
KAZAN_LON = 49.108795
KAZAN_LAT = 55.796289
KAZAN_RADIUS = 40000


async def fetch_page(session, api_key: str, page: int) -> dict:
    params = {
        "q": "жилой комплекс",
        "location": f"{KAZAN_LON},{KAZAN_LAT}",
        "radius": KAZAN_RADIUS,
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


async def main():
    if len(sys.argv) < 2:
        print("Usage: py -3 debug_2gis2.py YOUR_API_KEY")
        return

    api_key = sys.argv[1]
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE

    district_counts = {}
    matched = 0
    unmatched = 0

    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ssl_ctx)) as session:
        data = await fetch_page(session, api_key, 1)
        result = data.get("result", {})
        total = result.get("total", 0)
        total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        print(f"Total: {total}, pages: {total_pages}")

        all_items = list(result.get("items", []))
        for page in range(2, total_pages + 1):
            print(f"Page {page}/{total_pages}...", end="\r")
            data = await fetch_page(session, api_key, page)
            all_items.extend(data.get("result", {}).get("items", []))
            await asyncio.sleep(0.15)

    print(f"\nTotal items fetched: {len(all_items)}")

    for item in all_items:
        for div in item.get("adm_div", []):
            if div.get("type") == "district":
                raw = div.get("name", "").lower().strip()
                district_counts[raw] = district_counts.get(raw, 0) + 1
                if DISTRICT_NORMALIZE.get(raw):
                    matched += 1
                else:
                    unmatched += 1

    print(f"\nMatched to Kazan districts: {matched}")
    print(f"Unmatched (other cities/towns): {unmatched}")
    print(f"\nAll district names found:")
    for name, count in sorted(district_counts.items(), key=lambda x: -x[1]):
        mark = "OK" if DISTRICT_NORMALIZE.get(name) else "??"
        print(f"  [{mark}] {count:3d}x  {name!r}")


if __name__ == "__main__":
    asyncio.run(main())
