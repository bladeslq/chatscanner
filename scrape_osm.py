"""
Fetches residential complexes (ZhK) in Kazan from OpenStreetMap via Overpass API.
No API key needed. Outputs zhk_kazan.json + zhk_config_block.txt.
Usage: py -3 scrape_osm.py
"""
import asyncio
import aiohttp

OVERPASS_MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]

# Kazan bounding box (south, west, north, east)
KAZAN_BBOX = "55.68,48.90,55.92,49.35"

DISTRICT_NORMALIZE = {
    "вахитовский район": "Вахитовский",
    "советский район": "Советский",
    "приволжский район": "Приволжский",
    "кировский район": "Кировский",
    "московский район": "Московский",
    "ново-савиновский район": "Ново-Савиновский",
    "авиастроительный район": "Авиастроительный",
}


async def overpass_query(session: aiohttp.ClientSession, query: str) -> dict:
    for mirror in OVERPASS_MIRRORS:
        try:
            async with session.post(
                mirror,
                data={"data": query},
                timeout=aiohttp.ClientTimeout(total=120),
            ) as resp:
                if resp.status == 200:
                    return await resp.json(content_type=None)
                print(f"  {mirror} -> HTTP {resp.status}")
        except Exception as e:
            print(f"  {mirror} -> {e}")
    raise RuntimeError("Все зеркала недоступны")


def _approx_eq(a: tuple, b: tuple, eps: float = 1e-6) -> bool:
    return abs(a[0] - b[0]) < eps and abs(a[1] - b[1]) < eps


def assemble_rings(members: list) -> list[list[tuple]]:
    """Join way segments from an OSM relation into closed rings."""
    segs = []
    for m in members:
        if m.get("role") != "outer":
            continue
        pts = [(n["lon"], n["lat"]) for n in m.get("geometry", [])]
        if len(pts) >= 2:
            segs.append(pts)

    rings = []
    while segs:
        ring = list(segs.pop(0))
        changed = True
        while changed and not _approx_eq(ring[0], ring[-1]):
            changed = False
            for i in range(len(segs)):
                s = segs[i]
                if _approx_eq(s[0], ring[-1]):
                    ring.extend(s[1:])
                    segs.pop(i)
                    changed = True
                    break
                if _approx_eq(s[-1], ring[-1]):
                    ring.extend(s[-2::-1])
                    segs.pop(i)
                    changed = True
                    break
                if _approx_eq(s[-1], ring[0]):
                    ring = s[:-1] + ring
                    segs.pop(i)
                    changed = True
                    break
                if _approx_eq(s[0], ring[0]):
                    ring = s[::-1][:-1] + ring
                    segs.pop(i)
                    changed = True
                    break
        rings.append(ring)

    return rings


def point_in_polygon(lon: float, lat: float, polygon: list[tuple]) -> bool:
    inside = False
    n = len(polygon)
    j = n - 1
    for i in range(n):
        xi, yi = polygon[i]
        xj, yj = polygon[j]
        if ((yi > lat) != (yj > lat)) and (lon < (xj - xi) * (lat - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


def find_district(lon: float, lat: float, districts: list[dict]) -> str | None:
    for d in districts:
        for ring in d["rings"]:
            if point_in_polygon(lon, lat, ring):
                return d["name"]
    return None


def normalize_name(raw: str) -> str:
    name = raw.strip()
    for suffix in (
        ", жилой комплекс", ", жк", ", апартаменты",
        ", клубный дом", ", строящийся жилой комплекс",
    ):
        if name.lower().endswith(suffix):
            name = name[: -len(suffix)]
    for prefix in ("жилой комплекс ", "жк ", 'жк "', "жк«", "апартаменты ", "клубный дом "):
        if name.lower().startswith(prefix):
            name = name[len(prefix):]
    return name.strip().strip("«»\"'").lower()


async def main():
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False)) as session:

        # Step 1: district boundaries
        print("Загружаю границы районов Казани...")
        q = f"""
[out:json][timeout:60];
rel["admin_level"="9"]["boundary"="administrative"]({KAZAN_BBOX});
out geom;
"""
        data = await overpass_query(session, q)
        districts = []
        for elem in data.get("elements", []):
            name_raw = elem.get("tags", {}).get("name", "").lower().strip()
            district = DISTRICT_NORMALIZE.get(name_raw)
            if not district:
                continue
            rings = assemble_rings(elem.get("members", []))
            if rings:
                districts.append({"name": district, "rings": rings})
                print(f"  {district}: {len(rings)} кольцо(ец), {sum(len(r) for r in rings)} точек")

        if not districts:
            print("Не удалось загрузить границы районов.")
            return

        # Sanity check: Kremlin should be Вахитовский
        test_lon, test_lat = 49.1068, 55.7989
        test_result = find_district(test_lon, test_lat, districts)
        print(f"\nПроверка (Кремль): {test_result or 'НЕ НАЙДЕН'}")

        await asyncio.sleep(2)

        # Step 2: residential complexes
        print("\nЗагружаю ЖК из OpenStreetMap...")
        q2 = f"""
[out:json][timeout:90];
(
  way["landuse"="residential"]["name"]({KAZAN_BBOX});
  relation["landuse"="residential"]["name"]({KAZAN_BBOX});
  way["building"="apartments"]["name"]({KAZAN_BBOX});
  way["building"="residential"]["name"]({KAZAN_BBOX});
  relation["building"="apartments"]["name"]({KAZAN_BBOX});
  relation["type"="site"]["name"]({KAZAN_BBOX});
);
out center tags;
"""
        data = await overpass_query(session, q2)
        items = data.get("elements", [])
        print(f"  Объектов в OSM: {len(items)}")

    # Step 3: spatial join
    complexes: dict = {}
    no_district = 0

    for elem in items:
        name = elem.get("tags", {}).get("name", "").strip()
        if not name:
            continue
        center = elem.get("center") or {}
        lon = center.get("lon")
        lat = center.get("lat")
        if lon is None or lat is None:
            continue
        district = find_district(lon, lat, districts)
        if not district:
            no_district += 1
            continue
        key = normalize_name(name)
        if not key:
            continue
        complexes[key] = district

    print(f"\nИтого: {len(complexes)} ЖК с районом, без района: {no_district}")

    with open("zhk_kazan.json", "w", encoding="utf-8") as f:
        import json
        json.dump(complexes, f, ensure_ascii=False, indent=2)

    with open("zhk_config_block.txt", "w", encoding="utf-8") as f:
        f.write("KAZAN_COMPLEXES: dict[str, str] = {\n")
        for name, district in sorted(complexes.items()):
            escaped = name.replace('"', '\\"')
            f.write(f'    "{escaped}": "{district}",\n')
        f.write("}\n")

    print("Сохранено: zhk_kazan.json + zhk_config_block.txt")


if __name__ == "__main__":
    asyncio.run(main())
