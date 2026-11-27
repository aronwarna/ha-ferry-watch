"""Made-up answers for mapsources.py and buildings.py in the simulator (no network):
layer lists like the real services give, tiles that show their layer and year, and
buildings with years, so the Historic maps view and Building ages can be tried offline."""

from __future__ import annotations

import os

import io
import math
import random

from .mapsources import tile_box

FAKE_LISTS = {
    # As the real services listed them in Nov 2026 (a selection).
    "espoo": [("Ortokartta", "Espoon ortokartta 2026")] + [
        (f"Ortokartta_{y}", f"Espoon ortokartta {y}")
        for y in (2024, 2021, 2015, 2009, 2001, 1996, 1976, 1969, 1950)
    ] + [("Opaskartta", "Espoon opaskartta"), ("Opaskartta_1973", "Espoon opaskartta 1973"),
         ("Opaskartta_1976", "Espoon opaskartta 1976"), ("Kartta_1958", "Espoon kartta 1958"),
         ("Ortokartta_CIR", "Espoon vääräväriortokartta 2026"), ("PKS_Opaskartta", "PKS-Opaskartta")],
    "helsinki": [(f"avoindata:Ortoilmakuva_{y}", f"Ortoilmakuva {y}") for y in (1932, 1943, 1950, 1964, 1976, 1988, 2005)]
    + [("avoindata:Ortoilmakuva_2019_5cm", "Ortoilmakuva 2019 5 cm"), ("avoindata:Ortoilmakuva_2019_20cm", "x"),
       ("avoindata:Vaaravariortoilmakuva_2019", "CIR"), ("avoindata:Senaatinkartta_1870", "Senaatinkartta 1870"),
       ("avoindata:Rekognosointikartta_1780_1782", "Rekognosointikartta"), ("avoindata:Opaskartta_1965", "Opaskartta 1965"),
       ("avoindata:Helsingin_kaupunki_1917_1918", "Helsingin kaupunki 1917-1918"), ("avoindata:Kantakartta", "Kantakartta")],
    "vantaa": [("historia:orto_1954", "Ortoilmakuva 1954"), ("historia:orto_1976", "Ortoilmakuva 1976"),
               ("historia:orto_2011_cir", "Ortoilmakuva 2011 (vääräväri)"), ("historia:kuninkaankartta", "Kuninkaankartasto"),
               ("historia:senaattikartta_1872", "Senaatin kartta 1872"), ("historia:opaskartta1969", "Opaskartta 1969"),
               ("historia:peruskartta_1958", "Peruskartta 1958")],
    "mml-old": [("mml_ven_topo_21k_1871_1919", "MML, venäläinen topografinen kartta 1:21 000, 1871-1919"),
                ("mml_pitajakartta_20k_1912-1968", "MML, pitäjäkartta 1:20 000, 1912-1968"),
                ("mml_topo_20k_1947-1959", "MML, perus- tai topografinen kartta, 1:20 000, 1947-1959"),
                ("mml_topo_20k_1960-1969", "MML, perus- tai topografinen kartta, 1:20 000, 1960-1969"),
                ("mml_topo_20k_1980-1997", "MML, perus- tai topografinen kartta, 1:20 000, 1980-1997"),
                ("mml_taloudellinen_100k_1911-1970", "MML, taloudellinen kartta 1:100 000, 1911-1970"),
                ("mml_yleiskartta_400k_1863-1879", "MML, yleiskartta 1:400 000, 1863-1972"),
                ("mml_peruskartta_2022", "MML. Peruskartta 2022"), ("kotus_pitajat", "Kotus, Pitäjät, 1938")],
    "hsy-region": [("taustakartat_ja_aluejaot:ortoilmakuva2015", "Ortoilmakuva 2015"),
                   ("taustakartat_ja_aluejaot:Ortoilmakuva_2019", "Ortoilmakuva 2019"),
                   ("taustakartat_ja_aluejaot:Ortoilmakuva_2023", "Ortoilmakuva 2023"),
                   ("taustakartat_ja_aluejaot:Vaaravariortoilmakuva_2023", "Vääräväriortoilmakuva 2023")],
    "turku": [("Ilmakuva 2025", "Turun ilmakuva 2025"), ("Turku ilmakuva 2005", "Turun ilmakuva 2005"),
              ("Turku ilmakuva 1939", "Turun ilmakuva 1939"), ("Turku ilmakuva 2005 vaaravari", "x"),
              ("Turun Osoitekartta 1950", "Turun osoitekartta 1950"), ("Virastokartta 1958-61", "Virastokartta 1958-61"),
              ("Asemakaava", "Asemakaava")],
    "kirkkonummi": [(f"Historia_aineisto/{n}", "") for n in (
        "Ilmakuva_1944", "Ilmakuva_1954_1956", "Ilmakuva_1983", "ortokuva_2017", "Kuninkaankartasto_1776",
        "Pitäjänkartta_1932_1943", "Peruskartta_1960", "Parenteesialueen_raja")],
    "oulu": [("Ilmakuva 2012 Oulu", "Oulun ilmakuva 2012"), ("Ilmakuva 2024", "Ilmakuva 2024")],
    "estonia-old": [("of1993-2000_10k", "1993-2000 1:10 000"), ("of2005", "2005"), ("of2015aero", "Aeropildistamine 2015"),
                    ("of2015mets", "Metsanduslik 2015"), ("yheverstakaart", "Üheverstane kaart (1894-1922)"),
                    ("ew_25T", "Eesti topograafiline kaart 1:25 000 (1923-1935)"), ("nl_merekaart", "NL merekaart"),
                    ("ew_25T_info", "metainfo")],
    "emodnet-density": [("emodnet:2022_st_All_avg", "Vessel density 2022 all ship types"),
                        ("emodnet:2023_st_All_avg", "Vessel density 2023 all ship types"),
                        ("emodnet:2023_st_01_avg", "Vessel density 2023 fishing")],
}


def fake_capabilities(source: dict) -> list[dict]:
    return [{"name": n, "title": t, "box": source["box"]} for n, t in FAKE_LISTS.get(source["id"], [])]


def _sheets(row: dict) -> list[tuple[float, float, float, float]]:
    """Made-up coverage of a layer, in degrees (south, west, north, east): flight strips for
    photos (fewer and nearer the middle for older years), map sheets for maps."""
    s, w, n, e = row["box"]
    rnd = random.Random(row["id"])
    year = row.get("year") or 1950
    out = []
    if row["group"] == "photo":
        share = 0.35 + 0.6 * min(1.0, max(0.0, (year - 1930) / 70))
        strips = 5 + rnd.randrange(4)
        cy, cx = (s + n) / 2 + rnd.uniform(-0.15, 0.15) * (n - s), (w + e) / 2 + rnd.uniform(-0.15, 0.15) * (e - w)
        hh = (n - s) * share / 2
        for i in range(strips):
            y0 = cy - hh + 2 * hh * i / strips
            half = (e - w) * share / 2 * rnd.uniform(0.6, 1.0)
            off = rnd.uniform(-0.1, 0.1) * (e - w)
            out.append((y0, cx - half + off, y0 + 2 * hh / strips * 1.08, cx + half + off))
    else:
        sheets_y = 6 if (n - s) > 3 else 3
        sheets_x = 6 if (e - w) > 3 else 3
        dy, dx = (n - s) / sheets_y, (e - w) / sheets_x
        for i in range(sheets_y):
            for j in range(sheets_x):
                if rnd.random() < (0.75 if (n - s) > 3 else 0.85) or (i, j) == (0, 1):  # (0, 1): the south coast
                    out.append((s + i * dy, w + j * dx, s + (i + 1) * dy, w + (j + 1) * dx))
    return out


def fake_render(row: dict, src: dict, mbox, size: tuple[int, int]):
    """The picture a made-up WMS would send for a web-mercator box: the layer's sheets or
    strips, with what real services put round them: white (services that can't do
    see-through), black collars (photo mosaics), or nothing."""
    from PIL import Image, ImageDraw

    from .histclean import merc

    w, h = size
    img = Image.new("RGBA", size, (255, 255, 255, 255) if src.get("opaque") else (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    def px(lat: float, lon: float) -> tuple[float, float]:
        x, y = merc(lat, lon)
        return (x - mbox[0]) / (mbox[2] - mbox[0]) * w, (mbox[3] - y) / (mbox[3] - mbox[1]) * h

    sheets = _sheets(row)
    photo = row["group"] == "photo"
    year = row.get("year") or 1950
    if photo:
        for s, ww, n, e in sheets:  # the collar: a black frame round each strip
            pad = (n - s) * 0.12
            (x0, y1), (x1, y0) = px(s - pad, ww - pad), px(n + pad, e + pad)
            d.rectangle((x0, y0, x1, y1), fill=(0, 0, 0, 255))
    tone = 70 + min(140, max(0, (year - 1930) * 2))
    base = (tone, tone, tone - 8) if year < 1990 else (60, 100, 66)
    paper = (238, 228, 200) if year < 1960 else (246, 244, 236)
    for s, ww, n, e in sheets:
        (x0, y1), (x1, y0) = px(s, ww), px(n, e)
        d.rectangle((x0, y0, x1, y1), fill=(*(base if photo else paper), 255))
    # Texture tied to the ground (so it lines up from tile to tile), then the inside of the
    # coverage only: a fine grid of fields for photos, lines for maps.
    step = max(4.0, w / 24)
    rnd = random.Random(hash((row["id"], round(mbox[0]), round(mbox[1]))) & 0xFFFFFFFF)
    inside = Image.new("L", size, 0)
    di = ImageDraw.Draw(inside)
    for s, ww, n, e in sheets:
        (x0, y1), (x1, y0) = px(s, ww), px(n, e)
        di.rectangle((x0, y0, x1, y1), fill=255)
    tex = Image.new("RGBA", size, (0, 0, 0, 0))
    dt = ImageDraw.Draw(tex)
    for _ in range(int(w * h / 900)):
        cx, cy, r = rnd.uniform(0, w), rnd.uniform(0, h), rnd.uniform(2, step)
        shade = rnd.randrange(-35, 35)
        col = tuple(max(25, min(235, c + shade)) for c in (base if photo else (150, 110, 70)))
        if photo:
            dt.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(*col, 255))
        else:
            dt.line((cx - r, cy, cx + r, cy + r / 2), fill=(*col, 255), width=1)
    img.paste(tex, (0, 0), ImageChops_multiply(tex.getchannel("A"), inside))
    if w >= 256:
        d.text((8, 8), f"{row['where']} {row.get('year') or ''}", fill=(20, 20, 20, 255))
    return img


def ImageChops_multiply(a, b):  # noqa: N802 - Pillow's naming
    from PIL import ImageChops
    return ImageChops.multiply(a, b)


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


def fake_places(q: str) -> list[dict]:
    names = [("Rönnskär", 59.932, 24.395, "island"), ("Porkkalanniemi", 59.98, 24.42, "peninsula"),
             ("Kallbådan", 59.885, 24.585, "island"), ("Suomenlinna", 60.146, 24.987, "island"),
             ("Espoonlahti", 60.15, 24.64, "bay"), ("Pentala", 60.13, 24.68, "island")]
    ql = q.lower()
    return [{"name": n, "where": "Uusimaa, Suomi", "kind": k, "lat": a, "lon": b} for n, a, b, k in names if ql in n.lower()]


async def fake_ashore_fetch(query: str) -> dict:
    """Shelters, campfire sites and a sauna around Porkkala, as Overpass would answer."""
    els = [(3001, 59.9345, 24.3825, {"amenity": "shelter", "shelter_type": "lean_to", "name": "Rönnskärin laavu",
                                       "fireplace": "yes", "firewood": "yes"}),
           (3002, 59.9700, 24.4400, {"leisure": "firepit", "fee": "no"}),
           (3003, 59.9930, 24.4050, {"tourism": "wilderness_hut", "name": "Kallbåda kämppä", "toilets": "yes"}),
           (3004, 59.9790, 24.4310, {"leisure": "sauna", "name": "Rantasauna", "fee": "yes"}),
           (3005, 59.9600, 24.4100, {"amenity": "toilets"}),
           (3006, 60.2500, 24.0700, {"leisure": "firepit"})]  # inland: left out
    els += _dense_ashore(int(os.environ.get("FW_FAKE_ASHORE_N", "0") or 0))
    return {"elements": [{"type": "node", "id": i, "lat": a, "lon": b, "tags": t} for i, a, b, t in els]}


def _dense_ashore(n: int) -> list:
    """FW_FAKE_ASHORE_N=600: as crowded as OpenStreetMap is around Helsinki and Espoo, with
    clusters (a toilet, a tap and a firepit at one spot) along the Kirkkonummi-Helsinki shore."""
    import random
    rnd = random.Random(7)
    kinds = [({"amenity": "toilets"}, 5), ({"amenity": "drinking_water"}, 3), ({"leisure": "firepit"}, 2),
             ({"amenity": "shelter", "shelter_type": "lean_to"}, 1), ({"leisure": "sauna"}, 1),
             ({"tourism": "camp_site"}, 0.3), ({"amenity": "shelter", "shelter_type": "basic_hut"}, 0.3)]
    pool = [k for k, w in kinds for _ in range(int(w * 10))]
    out, i = [], 0
    while len(out) < n:
        lat, lon = rnd.uniform(59.94, 60.19), rnd.uniform(24.30, 25.05)
        for _ in range(rnd.choice([1, 1, 1, 2, 3, 4])):
            i += 1
            out.append((40000 + i, lat + rnd.gauss(0, 0.0008), lon + rnd.gauss(0, 0.0015), dict(rnd.choice(pool))))
    return out[:n]
