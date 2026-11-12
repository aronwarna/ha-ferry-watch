"""Made-up answers for mapsources.py and buildings.py in the simulator (no network):
layer lists like the real services give, tiles that show their layer and year, and
buildings with years, so the Then & now view and Building ages can be tried offline."""

from __future__ import annotations

import io
import math
import random

from .mapsources import tile_box

FAKE_LISTS = {
    "espoo": [("Ortokartta", "Espoon ortokartta 2026")] + [
        (f"Ortokartta_{y}", f"Espoon ortokartta {y}")
        for y in (2024, 2023, 2021, 2019, 2017, 2015, 2013, 2011, 2009, 2007, 2005, 2001, 1996, 1976, 1969, 1950)
    ] + [("Opaskartta", "Espoon opaskartta"), ("Opaskartta_1973", "Espoon opaskartta 1973"),
         ("Opaskartta_1976", "Espoon opaskartta 1976"), ("Espoon_kartta_1958", "Espoon kartta 1958"),
         ("Kantakartta", "Espoon kantakartta")],
    "helsinki": [(f"avoindata:Ortoilmakuva_{y}", f"Ortoilmakuva {y}") for y in (1932, 1943, 1950, 1964, 1976, 1988)]
    + [("avoindata:Ortoilmakuva_2019_5cm", "Ortoilmakuva 2019 5 cm"), ("avoindata:Ortoilmakuva_2019_20cm", "x"),
       ("avoindata:Senaatinkartta_1870", "Senaatinkartta 1870"), ("avoindata:Kantakartta", "Kantakartta")],
    "vantaa": [("ortoilmakuva_1956", "Ortoilmakuva 1956")],
    "mml-old": [("paituli:mml_peruskartta_1960", "Peruskartta 1:20 000 (1960)"),
                ("paituli:mml_senaatin_kartasto", "Senaatin kartasto 1870-1907"),
                ("paituli:mml_pitajankartta_1935", "Pitäjänkartta 1935"),
                ("paituli:corine", "Corine land cover")],
}


def fake_capabilities(source: dict) -> list[dict]:
    return [{"name": n, "title": t, "box": source["box"]} for n, t in FAKE_LISTS.get(source["id"], [])]


def fake_tile(row: dict, z: int, x: int, y: int) -> bytes:
    from PIL import Image, ImageDraw

    year = row.get("year") or 1900
    rnd = random.Random(hash((row["id"], z, x, y)) & 0xFFFFFFFF)
    if row["group"] == "photo":
        tone = 40 + min(160, max(0, (year - 1930) * 2))
        base = (110 + tone // 3, 105 + tone // 3, 95 + tone // 4) if year < 1990 else (70, 105, 70)
        img = Image.new("RGB", (256, 256), base)
        d = ImageDraw.Draw(img)
        for _ in range(60):
            cx, cy, r = rnd.randrange(256), rnd.randrange(256), rnd.randrange(3, 18)
            shade = rnd.randrange(-40, 40)
            d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=tuple(max(0, min(255, c + shade)) for c in base))
        if year < 1990:
            img = img.convert("L").convert("RGB")
    elif row["group"] == "map":
        img = Image.new("RGB", (256, 256), (238, 228, 200))
        d = ImageDraw.Draw(img)
        for i in range(0, 256, 32):
            d.line((i, 0, i, 256), fill=(200, 180, 150))
            d.line((0, i, 256, i), fill=(200, 180, 150))
        for _ in range(6):
            pts = [(rnd.randrange(256), rnd.randrange(256)) for _ in range(4)]
            d.line(pts, fill=(120, 80, 50), width=2)
    else:
        img = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        for i in range(0, 256, 64):
            d.line((i + rnd.randrange(30), 0, i + rnd.randrange(30), 256), fill=(200, 30, 30, 220), width=2)
    d = ImageDraw.Draw(img)
    d.text((8, 8), f"{row['where']} {row.get('year') or ''} {row['label']}", fill=(20, 20, 20))
    out = io.BytesIO()
    img.save(out, "PNG")
    return out.getvalue()


def fake_buildings(s: float, w: float, n: float, e: float) -> dict:
    rnd = random.Random(int(s * 1000) * 7919 + int(w * 1000))
    feats = []
    uses = ["Erilliset pientalot", "Rivitalot", "Asuinkerrostalot", "Liikerakennukset", "Saunarakennukset"]
    for _ in range(120):
        lat, lon = rnd.uniform(s, n), rnd.uniform(w, e)
        # Older near the old centres, newer further out: something to look at.
        year = int(1880 + 140 * (0.5 + 0.5 * math.sin(lat * 400 + lon * 300)) ** 1.5)
        feats.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [lon, lat]},
                      "properties": {"kavu": year, "katu": "Rantatie", "osno1": rnd.randrange(1, 60),
                                     "kayttarks": rnd.choice(uses), "kerrosten_lkm": rnd.randrange(1, 6),
                                     "julkisivu_s": rnd.choice(["Puu", "Tiili", "Betoni"]),
                                     "rakennusaine_s": rnd.choice(["Puu", "Betoni"])}})
    return {"type": "FeatureCollection", "features": feats}


def tile_centre(z: int, x: int, y: int) -> tuple[float, float]:
    s, w, n, e = tile_box(z, x, y)
    return (s + n) / 2, (w + e) / 2
