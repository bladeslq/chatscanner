"""Debug: print adm_div contents for skipped items to find correct type name."""
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


async def main():
    if len(sys.argv) < 2:
        print("Usage: py -3 debug_2gis.py YOUR_API_KEY")
        return

    api_key = sys.argv[1]
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE

    # Collect adm_div type names seen across all items
    type_counts = {}
    no_adm_div = 0
    sample_items = []  # first 5 items with no "district" type

    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ssl_ctx)) as session:
        # Only fetch pages 1-5 for quick debug
        for page in range(1, 6):
            print(f"Page {page}...")
            data = await fetch_page(session, api_key, page)
            items = data.get("result", {}).get("items", [])
            if not items:
                break
            for item in items:
                adm_div = item.get("adm_div", [])
                if not adm_div:
                    no_adm_div += 1
                    continue
                has_district_type = False
                for div in adm_div:
                    t = div.get("type", "")
                    type_counts[t] = type_counts.get(t, 0) + 1
                    if t == "district":
                        has_district_type = True
                if not has_district_type and len(sample_items) < 5:
                    sample_items.append({
                        "name": item.get("name"),
                        "adm_div": adm_div,
                    })
            await asyncio.sleep(0.15)

    print(f"\nadm_div type counts across sampled items:")
    for t, c in sorted(type_counts.items(), key=lambda x: -x[1]):
        print(f"  {t!r}: {c}")
    print(f"  (no adm_div at all): {no_adm_div}")

    print(f"\nSample items with no 'district' type (up to 5):")
    for s in sample_items:
        print(f"  {s['name']}")
        for div in s["adm_div"]:
            print(f"    type={div.get('type')!r} name={div.get('name')!r}")


if __name__ == "__main__":
    asyncio.run(main())
