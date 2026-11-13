"""Extra map layers from open map services: old aerial photos, old maps, and overlays
such as property boundaries, all fetched and kept by the NAS.

Everything the app knows about these services is in SOURCES below. To add a layer
later, add a source (or a rule to an existing one) there; the panel lists whatever
this module offers, so nothing else needs to change.

Each source is one of:

- "wms": an OGC WMS that can draw in web mercator (EPSG:3857). Its layers are found
  from its GetCapabilities once a day: every layer whose name matches one of the
  source's rules becomes a layer here, with the year the rule reads from its name or
  title. `known` lists layers to offer even before (or without) a GetCapabilities
  answer, so a service whose list can't be read still works.
- "xyz": a plain tile template ({z}/{x}/{y}, {key} for the Land Survey's API key).
  A list of templates is tried in order (the first that answers wins).
- "arcgis": an ArcGIS MapServer, drawn through its export call.

Layers come in three groups, which decide where the panel shows them:
"photo" (old aerial photos) and "map" (old maps) are the years of the Then & now
map type; "overlay" layers are switches in Layers, on top of any map type.

Historical pictures don't change, so tiles are kept for half a year, in their own
folder with their own size limit (they never push chart tiles out).
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import re
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any
from urllib.parse import quote

import aiohttp

from .config import USER_AGENT
from .tilecache import TileCache

_LOGGER = logging.getLogger(__name__)

HALF = 20037508.342789244
TILE_MAX_AGE = 182 * 86400
CACHE_BYTES = 600 * 1024 * 1024
DISCOVER_EVERY = 86400
CAPS_MAX_BYTES = 40 * 1024 * 1024  # a GetCapabilities answer larger than this is cut off

# Year ranges a rule's year must fall in (anything else in a name is not a year).
YEAR_RE = re.compile(r"(?<!\d)(1[5-9]\d\d|20[0-4]\d)(?!\d)")

ESPOO_BOX = (60.04, 24.47, 60.37, 24.86)     # south, west, north, east
HELSINKI_BOX = (60.06, 24.78, 60.31, 25.27)
VANTAA_BOX = (60.24, 24.68, 60.42, 25.20)
FINLAND_BOX = (59.3, 19.0, 70.2, 31.7)
NORTH_BOX = (53.0, 9.0, 71.0, 33.0)          # Finland, the Baltic and its other shores

# fmt: off
SOURCES: list[dict[str, Any]] = [
    {
        "id": "espoo", "name": "Espoo", "kind": "wms",
        "url": "https://kartat.espoo.fi/TeklaOgcWeb/WMS.ashx",
        "attribution": "© Espoon kaupunki (CC BY 4.0)",
        "box": ESPOO_BOX, "max_zoom": 19,
        "rules": [
            # Ortokartta_1969 … Ortokartta_2024; plain "Ortokartta" is the newest (2026).
            {"match": r"^Ortokartta_(\d{4})$", "group": "photo", "format": "image/jpeg"},
            {"match": r"^Ortokartta$", "group": "photo", "year": 2026, "format": "image/jpeg"},
            # Guide maps of 1958, 1973, 1976 (names not known yet: any guide map with a year).
            {"match": r"(?i)opas.*\d{4}|\d{4}.*opas|kartta.*_19\d\d", "group": "map"},
            {"match": r"^Opaskartta$", "group": "map", "year": 2026, "label": "Guide map"},
        ],
        "known": ["Ortokartta", "Ortokartta_2024", "Ortokartta_2023", "Ortokartta_2021", "Ortokartta_2019",
                  "Ortokartta_2017", "Ortokartta_2015", "Ortokartta_2013", "Ortokartta_2011", "Ortokartta_2009",
                  "Ortokartta_2007", "Ortokartta_2005", "Ortokartta_2001", "Ortokartta_1996", "Ortokartta_1976",
                  "Ortokartta_1969", "Opaskartta"],
    },
    {
        "id": "helsinki", "name": "Helsinki", "kind": "wms",
        "url": "https://kartta.hel.fi/ws/geoserver/avoindata/wms",
        "attribution": "© Helsingin kaupunki (CC BY 4.0)",
        "box": HELSINKI_BOX, "max_zoom": 19,
        "rules": [
            # One picture per year: plain names first, then the coarsest resolution.
            {"match": r"^(?:avoindata:)?Ortoilmakuva_(\d{4})(?:_(\d+)cm)?$", "group": "photo", "format": "image/jpeg"},
            {"match": r"(?i)^(?:avoindata:)?(Pitajakuvauskartta|Rekognosointikartta|Senaatinkartta|Plan_af_Helsingfors"
                      r"|Kartta_Helsingin|Helsingin_kaupunki_19|Helsingin_kaupungin_kartta)", "group": "map"},
            {"match": r"(?i)^(?:avoindata:)?.*opaskartta.*_(19\d\d)$", "group": "map"},
        ],
        "known": ["avoindata:Ortoilmakuva_" + y for y in (
                      "1932", "1943", "1950", "1954", "1956", "1961", "1964", "1969", "1972", "1976", "1987",
                      "1988", "1991", "1993", "1995", "1996", "1997", "1998", "2001", "2005", "2010", "2012",
                      "2014", "2016", "2018", "2020")]
                 + ["avoindata:" + n for n in (
                      "Pitajakuvauskartta_1749", "Rekognosointikartta_1780_1782", "Senaatinkartta_1870",
                      "Plan_af_Helsingfors_1900", "Kartta_Helsingin_kaupungista_ja_sen_alueista_1909",
                      "Helsingin_kaupunki_1917_1918", "Helsingin_kaupungin_kartta_1925")],
    },
    {
        "id": "vantaa", "name": "Vantaa", "kind": "wms",
        "url": "https://gis.vantaa.fi/geoserver/ows",
        "attribution": "© Vantaan kaupunki (CC BY 4.0)",
        "box": VANTAA_BOX, "max_zoom": 19, "discover_only": True,
        "rules": [
            {"match": r"(?i)orto.*(?:19\d\d|200\d)", "group": "photo", "format": "image/jpeg"},
            {"match": r"(?i)(kartta|map|kartasto).*1[789]\d\d", "group": "map"},
        ],
    },
    {
        # The Land Survey's old printed maps (1860s to 2020, ~30,000 sheets, CC BY 4.0),
        # georeferenced and published through CSC's Paituli. Which of them Paituli's WMS
        # draws is read from its GetCapabilities.
        "id": "mml-old", "name": "Land Survey old maps", "kind": "wms",
        "url": "https://paituli.csc.fi/geoserver/paituli/wms",
        "attribution": "Old maps © Maanmittauslaitos (CC BY 4.0), via CSC Paituli",
        "box": FINLAND_BOX, "max_zoom": 17, "discover_only": True,
        "rules": [
            {"match": r"(?i)(vanha|old_?map|histor|senaat|pitajan|pitäjän|venal|venäl|russian|kalmberg"
                      r"|yleiskart|peruskart|taloudellinen|topografinen)", "group": "map",
             "title_too": True, "need_year": True},
            # Older vintages of the Land Survey's own orthophotos, where Paituli's WMS draws them.
            {"match": r"(?i)orto", "exclude": r"(?i)vaara|vääräv|cir|infra|index|ruudu|lehtijako", "group": "photo",
             "format": "image/jpeg", "title_too": True, "year_required": True},
        ],
    },
    {
        # The capital region's joint photo flights (HSY): also Kirkkonummi with Porkkala, Kerava,
        # Järvenpää, Tuusula, Nurmijärvi and Sipoo, 2015 onwards. Layer names are read from the list.
        "id": "hsy-region", "name": "Helsinki region", "kind": "wms",
        "url": "https://kartta.hsy.fi/geoserver/wms",
        "attribution": "Orthophoto © the region's municipalities, HSY, HSL and the Defence Forces (CC BY 4.0)",
        "box": (59.90, 24.20, 60.65, 25.50), "max_zoom": 19, "discover_only": True,
        "rules": [
            {"match": r"(?i)orto.*(20\d\d)", "exclude": r"(?i)cir|vaara|vääräv|infra|nir", "group": "photo",
             "format": "image/jpeg", "title_too": True, "year_required": True},
        ],
    },
    {
        "id": "turku", "name": "Turku", "kind": "wms",
        "url": "https://turku.asiointi.fi/teklaogcweb/wms.ashx",
        "attribution": "© Turun kaupunki (CC BY 4.0)",
        "box": (60.28, 21.90, 60.62, 22.75), "max_zoom": 19,
        "rules": [
            {"match": r"(?i)ilmakuva.*\d{4}|orto.*\d{4}", "exclude": r"(?i)vaara|vääräv|cir", "group": "photo",
             "format": "image/jpeg", "title_too": True, "year_required": True},
            {"match": r"(?i)(kartta|opas|asemakaava).*1[6-9]\d\d", "group": "map", "title_too": True, "year_required": True},
        ],
        "known": ["Ilmakuva 2025", "Ilmakuva 2023", "Ilmakuva 2021", "Turunseutu ilmakuva 2018",
                  "Turunseutu ilmakuva 2013", "Turku ilmakuva 2010", "Turku ilmakuva 2005"],
    },
    {
        "id": "tampere", "name": "Tampere", "kind": "wms",
        "url": "https://geodata.tampere.fi/geoserver/ows",
        "attribution": "© Tampereen kaupunki (CC BY 4.0)",
        "box": (61.35, 23.45, 61.85, 24.15), "max_zoom": 19, "discover_only": True,
        "rules": [
            {"match": r"(?i)(ilmakuva|orto).*(19|20)\d\d", "exclude": r"(?i)vaara|vääräv|cir|index|ruudu", "group": "photo",
             "format": "image/jpeg", "title_too": True, "year_required": True},
            {"match": r"(?i)(kartta|opas|asemakaava).*1[6-9]\d\d", "group": "map", "title_too": True, "year_required": True},
        ],
    },
    {
        "id": "oulu", "name": "Oulu", "kind": "wms",
        "url": "https://e-kartta.ouka.fi/TeklaOgcWebOpen/WMS.ashx",
        "attribution": "© Oulun kaupunki (CC BY 4.0)",
        "box": (64.80, 24.90, 65.60, 26.80), "max_zoom": 19,
        "rules": [
            {"match": r"(?i)ilmakuva.*\d{4}|orto.*\d{4}", "exclude": r"(?i)vaara|vääräv|cir", "group": "photo",
             "format": "image/jpeg", "title_too": True, "year_required": True},
        ],
        "known": ["Ilmakuva 2024", "Ilmakuva 2018 re", "Ilmakuva 2012 Oulu"],
    },
    {
        # Estonia's Land Board: every orthophoto flight since 1993 and old maps back to the
        # 1800s (verst maps, 1920s-30s Estonian and Soviet topographic maps), free with credit.
        "id": "estonia-old", "name": "Estonia", "kind": "wms",
        "url": "https://kaart.maaamet.ee/wms/ajalooline",
        "attribution": "© Maa- ja Ruumiamet (Estonian Land and Spatial Development Board)",
        "box": (57.5, 21.7, 59.75, 28.3), "max_zoom": 18, "discover_only": True,
        "rules": [
            {"match": r"(?i)orto|foto", "exclude": r"(?i)nir|cir|infra|ruudu|index|lehed", "group": "photo",
             "format": "image/jpeg", "title_too": True, "year_required": True},
            {"match": r"(?i)verst|topo|kaart|map|schmidt|r[üu]cker|heeres|kataster", "exclude": r"(?i)ruudu|index|lehed",
             "group": "map", "title_too": True, "year_required": True},
        ],
    },
    {
        # Sentinel-2 cloudless (EOX): one cloud-free satellite picture of the whole region per
        # year, 10 m. Coarse, but it covers every place the city photo flights don't.
        "id": "s2", "name": "Sentinel-2 satellite", "kind": "xyz", "group": "photo",
        "title": "Satellite (10 m)",
        "years": {y: f"https://tiles.maps.eox.at/wmts/1.0.0/s2cloudless-{y}_3857/default/g/{{z}}/{{y}}/{{x}}.jpg"
                  for y in range(2018, 2025)},
        "attribution": "Sentinel-2 cloudless by EOX (modified Copernicus Sentinel data, CC BY-NC-SA 4.0)",
        "box": NORTH_BOX, "min_zoom": 3, "max_zoom": 14, "wide": True,
    },
    {
        # Esri's World Imagery archive ("Wayback"): what the satellite view showed at each past
        # release since 2014, everywhere, down to house level. One release a year is offered
        # (the year's last); the list of releases is read from Esri's own config once a day.
        "id": "wayback", "name": "Esri imagery archive", "kind": "wayback", "group": "photo",
        "title": "Satellite archive",
        "url": "https://s3-us-west-2.amazonaws.com/config.maptiles.arcgis.com/waybackconfig.json",
        "template": "https://wayback.maptiles.arcgis.com/arcgis/rest/services/World_Imagery/WMTS/1.0.0/"
                    "default028mm/MapServer/tile/{release}/{z}/{y}/{x}",
        "attribution": "Imagery © Esri, Maxar, Earthstar Geographics (World Imagery Wayback)",
        "box": NORTH_BOX, "min_zoom": 3, "max_zoom": 18, "wide": True, "discover_only": True,
    },
    {
        "id": "property", "name": "Property boundaries", "kind": "xyz", "group": "overlay",
        "title": "Property boundaries", "info": "Land Survey's cadastral map: property borders, from zoom 14.",
        "templates": [
            "https://avoin-karttakuva.maanmittauslaitos.fi/avoin/wmts/1.0.0/kiinteistojaotus/default/"
            "WGS84_Pseudo-Mercator/{z}/{y}/{x}.png?api-key={key}",
            "https://tiles.kartat.kapsi.fi/kiinteistorajat/{z}/{x}/{y}.png",
        ],
        "attribution": "Property boundaries © Maanmittauslaitos (CC BY 4.0)",
        "box": FINLAND_BOX, "min_zoom": 14, "max_zoom": 16, "z_shown": 14, "opacity": 0.75,
    },
    {
        "id": "property-ids", "name": "Property ids", "kind": "xyz", "group": "overlay",
        "title": "Property ids", "info": "Each property's register id (e.g. 257-411-1-23), from zoom 15. Tap one to copy it.",
        "templates": [
            "https://avoin-karttakuva.maanmittauslaitos.fi/avoin/wmts/1.0.0/kiinteistotunnukset/default/"
            "WGS84_Pseudo-Mercator/{z}/{y}/{x}.png?api-key={key}",
        ],
        "needs_key": True,
        "attribution": "Property ids © Maanmittauslaitos (CC BY 4.0)",
        "box": FINLAND_BOX, "min_zoom": 16, "max_zoom": 16, "z_shown": 16,
    },
    {
        "id": "seabed", "name": "Seabed type", "kind": "arcgis", "group": "overlay",
        "title": "Seabed type (GTK)", "info": "Geological Survey's map of what the seabed is: mud, clay, sand, "
                                               "gravel, rock. Good to know where to anchor.",
        "url": "https://gtkdata.gtk.fi/arcgis/rest/services/Maankamara/Merenpohja/MapServer/export",
        "attribution": "Seabed © GTK (CC BY 4.0)",
        "box": (59.3, 19.0, 66.0, 30.5), "min_zoom": 8, "max_zoom": 16, "opacity": 0.6,
    },
    {
        # NASA's satellite picture of the whole Earth, new each day (VIIRS, ~375 m): sea ice,
        # algae blooms, snow and clouds as they were today. "{date}" is today's (UTC) after the
        # afternoon pass over Finland, else yesterday's.
        "id": "gibs-today", "name": "NASA GIBS", "kind": "xyz", "group": "overlay",
        "title": "Satellite today (NASA)", "info": "Yesterday's or today's satellite picture (about 375 m), best "
                                                  "zoomed out: ice, algae blooms, snow and clouds as they really "
                                                  "were. Dark or missing in midwinter, when the sun is too low.",
        # Tried in order, each for today's picture and then the day before's: the first
        # satellite that has one wins (satellites are retired and pictures arrive late).
        "templates": [
            f"https://gibs.earthdata.nasa.gov/wmts/epsg3857/best/{product}_CorrectedReflectance_TrueColor/default/"
            f"{{{day}}}/GoogleMapsCompatible_Level9/{{z}}/{{y}}/{{x}}.jpg"
            for day in ("date", "date1")
            for product in ("VIIRS_NOAA20", "VIIRS_NOAA21", "VIIRS_SNPP", "MODIS_Terra", "MODIS_Aqua")
        ],
        "daily": True, "max_age": 3 * 86400,
        "attribution": "Satellite © NASA EOSDIS GIBS",
        "box": (53.0, 9.0, 71.0, 33.0), "min_zoom": 3, "max_zoom": 9, "opacity": 1,
    },
    {
        # EMODnet's map of where ships go (AIS hours per km² a month, all ship types), the
        # newest year the service has. Layer names are read from its list.
        "id": "emodnet-density", "name": "EMODnet Human Activities", "kind": "wms", "group": "overlay",
        "url": "https://ows.emodnet-humanactivities.eu/wms",
        "attribution": "Vessel density © EMODnet Human Activities (CC BY 4.0)",
        "box": (53.0, 9.0, 66.0, 31.0), "min_zoom": 3, "max_zoom": 12, "discover_only": True,
        "rules": [
            {"match": r"(?i)(vessel.?density|vesseldensity|_st_all_avg).*all|all.*(vessel.?density|_avg)",
             "group": "overlay", "title_too": True, "label": "Ship traffic density", "latest_only": True,
             "opacity": 0.8, "info": "Where ships go: EMODnet's yearly map of AIS hours per km² (all ship types). "
                                     "Fairways and ferry lanes stand out."},
        ],
    },
]
# fmt: on


def daily_date(now: float | None = None) -> str:
    """The day whose daily satellite picture to show: today's (UTC) once the early
    afternoon passes over the Baltic are in (13 UTC), else yesterday's."""
    return time.strftime("%Y-%m-%d", time.gmtime((now if now is not None else time.time()) - 13 * 3600))


def mercator_bbox(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    size = 2 * HALF / 2**z
    x0, y1 = -HALF + x * size, HALF - y * size
    return x0, y1 - size, x0 + size, y1


def tile_box(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    """(south, west, north, east) of a web-mercator tile in degrees."""
    def lat(t: float) -> float:
        return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * t / 2**z))))
    return lat(y + 1), x / 2**z * 360 - 180, lat(y), (x + 1) / 2**z * 360 - 180


def overlaps(a: tuple, b: tuple) -> bool:
    return a[0] < b[2] and a[2] > b[0] and a[1] < b[3] and a[3] > b[1]


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60]


def _pretty(name: str) -> str:
    """A layer name made readable: "avoindata:Senaatinkartta_1870" -> "Senaatinkartta 1870"."""
    return name.split(":")[-1].replace("_", " ").strip()


MAP_NAMES = {  # nicer English titles for well-known old maps
    "pitajakuvauskartta": "Parish map", "rekognosointikartta": "Reconnaissance map",
    "senaatinkartta": "Senate map", "plan af helsingfors": "Plan of Helsingfors",
    "kartta helsingin kaupungista": "Map of Helsinki", "helsingin kaupunki": "Map of Helsinki",
    "helsingin kaupungin kartta": "Map of Helsinki", "opaskartta": "Guide map",
    "espoon kartta": "Map of Espoo", "senaatin": "Senate map", "pitajan": "Parish map", "pitäjän": "Parish map",
    "peruskartta": "Basic map", "yleiskartta": "General map", "venal": "Russian topographic map",
    "venäl": "Russian topographic map", "russian": "Russian topographic map", "kalmberg": "Kalmberg's map",
    "taloudellinen": "Economic map", "topografinen": "Topographic map",
}


def _label(name: str, title: str, group: str) -> str:
    if group == "photo":
        return "Aerial photo"
    text = (title or _pretty(name)).lower()
    for key, label in MAP_NAMES.items():
        if key in text:
            return label
    text = re.sub(r"\(\s*[-–]?\s*\)", "", YEAR_RE.sub("", title or _pretty(name)))
    return re.sub(r"\s+", " ", text).strip(" -–_,") or "Old map"


def layers_from_names(source: dict, found: list[dict]) -> list[dict]:
    """Turn a source's service layers ({name, title, box}) into our layers, by its rules.
    One layer per (group, year) per source: a photo given at several resolutions is
    taken once, at its plainest name or else its coarsest resolution (faster to draw)."""
    picked: dict[tuple, tuple] = {}
    for entry in found:
        name, title = entry["name"], entry.get("title") or ""
        for rule in source["rules"]:
            text = f"{name} {title}" if rule.get("title_too") else name
            m = re.search(rule["match"], text)
            if not m:
                continue
            year = rule.get("year")
            if year is None:
                years = YEAR_RE.findall(name) or YEAR_RE.findall(title)
                year = int(years[0]) if years else None
            if rule.get("exclude") and re.search(rule["exclude"], text):
                continue
            if rule.get("year_required") and year is None:
                continue
            if rule.get("need_year") and (year is None or year > 2020):
                continue  # today's maps aren't old maps
            group = rule["group"]
            label = rule.get("label") or _label(name, title, group)
            # Prefer the plain name, then larger cm per pixel (two groups in the photo rule).
            cm = m.group(2) if group == "photo" and m.re.groups >= 2 and m.group(2) else None
            rank = (0 if cm is None else 1, -(int(cm) if cm else 0))
            key = (group, year, label if group != "photo" else "")
            if key in picked and picked[key][0] <= rank:
                break
            box = entry.get("box") or source["box"]
            picked[key] = (rank, {
                "id": f"{source['id']}-{_slug(name.split(':')[-1])}",
                "source": source["id"], "where": source["name"], "group": group, "year": year,
                "label": label, "title": title or _pretty(name), "layer": name,
                "format": rule.get("format", "image/png"), "box": [round(v, 4) for v in box],
                "min_zoom": source.get("min_zoom", 8), "max_zoom": source.get("max_zoom", 18),
                "attribution": source["attribution"], "info": rule.get("info", ""),
                "opacity": rule.get("opacity", 1),
            })
            break
    # Rules that want only the newest year (e.g. yearly traffic maps) keep one layer.
    for rule in source["rules"]:
        if rule.get("latest_only"):
            same = [k for k in picked if k[0] == rule["group"] and k[2] == (rule.get("label") or "")]
            newest = max(same, key=lambda k: k[1] or 0, default=None)
            for k in same:
                if k != newest:
                    del picked[k]
    return [v for _, v in sorted(picked.values(), key=lambda p: (p[1]["group"], p[1]["year"] or 0, p[1]["label"]))]


def parse_capabilities(path: Path) -> list[dict]:
    """The named layers of a WMS GetCapabilities document (1.1.1 or 1.3.0), with their
    titles and lat/lon boxes, read piece by piece so a big document never sits in RAM."""
    out: list[dict] = []
    stack: list[dict] = []
    for event, el in ET.iterparse(path, events=("start", "end")):
        tag = el.tag.rsplit("}", 1)[-1]
        if event == "start":
            if tag == "Layer":
                stack.append({})
            continue
        if not stack:
            el.clear()
            continue
        cur = stack[-1]
        if tag == "Name" and "name" not in cur:
            cur["name"] = (el.text or "").strip()
        elif tag == "Title" and "title" not in cur:
            cur["title"] = (el.text or "").strip()
        elif tag == "LatLonBoundingBox":  # 1.1.1
            try:
                cur["box"] = (float(el.get("miny")), float(el.get("minx")), float(el.get("maxy")), float(el.get("maxx")))
            except (TypeError, ValueError):
                pass
        elif tag == "EX_GeographicBoundingBox":  # 1.3.0
            vals = {c.tag.rsplit("}", 1)[-1]: c.text for c in el}
            try:
                cur["box"] = (float(vals["southBoundLatitude"]), float(vals["westBoundLongitude"]),
                              float(vals["northBoundLatitude"]), float(vals["eastBoundLongitude"]))
            except (KeyError, TypeError, ValueError):
                pass
        elif tag == "Layer":
            done = stack.pop()
            if stack and "box" in stack[-1] and "box" not in done:
                done["box"] = stack[-1]["box"]  # a layer inherits its parent's box
            if done.get("name"):
                out.append(done)
        if tag in ("Layer", "Name", "Title", "EX_GeographicBoundingBox", "LatLonBoundingBox"):
            el.clear()
    return out


def _wide_row(source: dict, year: int, **more: Any) -> dict:
    return {"id": f"{source['id']}-{year}", "source": source["id"], "where": source["name"], "group": source["group"],
            "year": year, "label": source["title"], "title": f"{source['title']} {year}", "box": list(source["box"]),
            "min_zoom": source.get("min_zoom", 0), "max_zoom": source.get("max_zoom", 18), "wide": True,
            "attribution": source["attribution"], **more}


def wayback_releases(config: dict) -> dict[int, int]:
    """Esri's Wayback config ({release number: {itemTitle: "World Imagery (Wayback 2019-06-26)", …}})
    -> {year: release number}, each year's last release."""
    best: dict[int, tuple[str, int]] = {}
    for num, item in (config or {}).items():
        m = re.search(r"(\d{4})-(\d\d)-(\d\d)", str((item or {}).get("itemTitle", "")))
        try:
            release = int(num)
        except (TypeError, ValueError):
            continue
        if m and (int(m.group(1)) not in best or m.group(0) > best[int(m.group(1))][0]):
            best[int(m.group(1))] = (m.group(0), release)
    return {year: rel for year, (_, rel) in sorted(best.items())}


def known_layers(source: dict) -> list[dict]:
    return layers_from_names(source, [{"name": n} for n in source.get("known", [])])


class MapSources:
    """The extra layers, their tiles, and what Settings shows about each service."""

    def __init__(self, directory: Path, session: aiohttp.ClientSession, store, fake: bool = False):
        self.dir = directory
        self.session = session
        self.store = store
        self.fake = fake
        self.cache = TileCache(directory / "tiles", session, CACHE_BYTES)
        for s in SOURCES:
            self.cache.max_ages[s["id"]] = s.get("max_age", TILE_MAX_AGE)
        self.found: dict[str, list[dict]] = self.store.kv_get("mapsources_found", {}) or {}
        self.checked: dict[str, dict] = {}
        self._layers: dict[str, dict] = {}
        self._rebuild()

    # --- the list ---------------------------------------------------------------------
    def _rebuild(self) -> None:
        layers: dict[str, dict] = {}
        for s in SOURCES:
            if s["kind"] == "wms":
                found = self.found.get(s["id"])
                rows = layers_from_names(s, found) if found else ([] if s.get("discover_only") else known_layers(s))
                for row in rows:
                    layers[row["id"]] = row
            elif s.get("years"):
                for year in s["years"]:
                    row = _wide_row(s, year)
                    layers[row["id"]] = row
            elif s["kind"] == "wayback":
                for year, release in (self.found.get(s["id"]) or {}).items():
                    row = _wide_row(s, int(year), release=release)
                    layers[row["id"]] = row
            else:
                layers[s["id"]] = {
                    "id": s["id"], "source": s["id"], "where": s["name"], "group": s["group"], "year": None,
                    "label": s["title"], "title": s["title"], "info": s.get("info", ""),
                    "box": list(s["box"]), "min_zoom": s.get("min_zoom", 0), "max_zoom": s.get("max_zoom", 18),
                    "z_shown": s.get("z_shown", s.get("min_zoom", 0)), "opacity": s.get("opacity", 1),
                    "attribution": s["attribution"],
                }
                if s.get("daily"):
                    layers[s["id"]]["daily"] = True
        self._layers = layers

    def row(self, layer_id: str) -> dict | None:
        return self._layers.get(layer_id)

    def listing(self) -> dict:
        key = bool(self.store.kv_get("mml_api_key", ""))
        rows = []
        for row in self._layers.values():
            src = next(s for s in SOURCES if s["id"] == row["source"])
            if src.get("needs_key") and not key and not self.fake:
                continue
            rows.append({**row, "daily": daily_date()} if row.get("daily") else row)
        return {"layers": rows, "sources": self.sources()}

    def sources(self) -> list[dict]:
        """Settings' rows: each service, how many layers it gave, and whether it fails."""
        out = []
        for s in SOURCES:
            n = sum(1 for r in self._layers.values() if r["source"] == s["id"])
            check = self.checked.get(s["id"], {})
            out.append({"id": s["id"], "name": s["name"], "layers": n,
                        "failing": self.cache.failing.get(s["id"]) or check.get("error"),
                        "checked": check.get("ts")})
        return out

    # --- finding layers ---------------------------------------------------------------
    async def run(self) -> None:
        """Read each WMS's layer list at start and once a day."""
        while True:
            for s in SOURCES:
                if s["kind"] == "wms":
                    await self.discover(s)
                elif s["kind"] == "wayback":
                    await self.discover_wayback(s)
            self._rebuild()
            await asyncio.sleep(DISCOVER_EVERY)

    async def discover_wayback(self, s: dict) -> None:
        try:
            if self.fake:
                config = {str(10 + i * 100): {"itemTitle": f"World Imagery (Wayback {2014 + i}-06-1{i % 9})"} for i in range(12)}
            else:
                async with self.session.get(s["url"], headers={"User-Agent": USER_AGENT},
                                            timeout=aiohttp.ClientTimeout(total=60)) as resp:
                    resp.raise_for_status()
                    config = await resp.json(content_type=None)
            releases = wayback_releases(config)
            if not releases:
                raise ValueError("no releases in the list")
        except Exception as err:  # noqa: BLE001 - any failure keeps the last good list
            _LOGGER.warning("Map layers from %s: couldn't read the release list: %s", s["name"], err)
            self.checked[s["id"]] = {"ts": time.time(), "error": str(err) or type(err).__name__}
            return
        _LOGGER.info("Map layers from %s: %s years (%s to %s)", s["name"], len(releases), min(releases), max(releases))
        self.found[s["id"]] = {str(y): r for y, r in releases.items()}
        self.store.kv_set("mapsources_found", self.found)
        self.checked[s["id"]] = {"ts": time.time(), "error": None}

    async def discover(self, s: dict) -> None:
        url = f"{s['url']}?SERVICE=WMS&REQUEST=GetCapabilities&VERSION=1.1.1"
        try:
            if self.fake:
                from .simmaps import fake_capabilities
                found = fake_capabilities(s)
            else:
                found = await self._capabilities(url)
        except Exception as err:  # noqa: BLE001 - any failure keeps the last good list
            _LOGGER.warning("Map layers from %s: couldn't read the layer list: %s", s["name"], err)
            self.checked[s["id"]] = {"ts": time.time(), "error": str(err) or type(err).__name__}
            return
        rows = layers_from_names(s, found)
        _LOGGER.info("Map layers from %s: %s of %s layers taken (%s)", s["name"], len(rows), len(found),
                     ", ".join(f"{r['group']} {r['year'] or ''} {r['label']}".strip() for r in rows[:40]))
        if not rows and s.get("rules"):
            names = ", ".join(e["name"] for e in found[:60])
            _LOGGER.info("Map layers from %s: none matched; the service has: %s", s["name"], names)
        self.found[s["id"]] = [{k: v for k, v in e.items() if k in ("name", "title", "box")}
                               for e in found if layers_from_names(s, [e])]
        self.store.kv_set("mapsources_found", self.found)
        self.checked[s["id"]] = {"ts": time.time(), "error": None}

    async def _capabilities(self, url: str) -> list[dict]:
        fd, name = tempfile.mkstemp(prefix="caps-", suffix=".xml", dir=self.dir if self.dir.exists() else None)
        path = Path(name)
        try:
            size = 0
            with os.fdopen(fd, "wb") as fh:
                async with self.session.get(url, headers={"User-Agent": USER_AGENT},
                                            timeout=aiohttp.ClientTimeout(total=120)) as resp:
                    resp.raise_for_status()
                    async for chunk in resp.content.iter_chunked(65536):
                        size += len(chunk)
                        if size > CAPS_MAX_BYTES:
                            raise ValueError("layer list too large")
                        fh.write(chunk)
            return await asyncio.to_thread(parse_capabilities, path)
        finally:
            path.unlink(missing_ok=True)

    # --- tiles ------------------------------------------------------------------------
    def covers(self, layer_id: str, z: int, x: int, y: int) -> bool | None:
        """True if the layer may have a picture here, False if never (outside its area or
        zooms), None if there's no such layer."""
        row = self._layers.get(layer_id)
        if not row:
            return None
        if not (row["min_zoom"] <= z <= row["max_zoom"] and 0 <= x < 2**z and 0 <= y < 2**z):
            return False
        return overlaps(tile_box(z, x, y), tuple(row["box"]))

    def tile_urls(self, row: dict, z: int, x: int, y: int) -> list[str]:
        src = next(s for s in SOURCES if s["id"] == row["source"])
        if src["kind"] == "wms":
            bbox = ",".join(f"{v:.3f}" for v in mercator_bbox(z, x, y))
            fmt = row["format"]
            return [f"{src['url']}?SERVICE=WMS&VERSION=1.1.1&REQUEST=GetMap&LAYERS={quote(row['layer'], safe=':,')}&STYLES="
                    f"&SRS=EPSG:3857&BBOX={bbox}&WIDTH=256&HEIGHT=256&FORMAT={fmt}"
                    f"&TRANSPARENT={'FALSE' if fmt == 'image/jpeg' else 'TRUE'}"]
        if src["kind"] == "arcgis":
            bbox = ",".join(f"{v:.3f}" for v in mercator_bbox(z, x, y))
            return [f"{src['url']}?bbox={bbox}&bboxSR=3857&imageSR=3857&size=256,256&format=png32"
                    f"&transparent=true&f=image"]
        if src.get("years"):
            return [src["years"][row["year"]].format(z=z, x=x, y=y)]
        if src["kind"] == "wayback":
            return [src["template"].format(release=row["release"], z=z, x=x, y=y)]
        key = self.store.kv_get("mml_api_key", "")
        date, date1 = daily_date(), daily_date(time.time() - 86400)
        return [t.format(z=z, x=x, y=y, key=key, date=date, date1=date1) for t in src["templates"]
                if key or "{key}" not in t]

    async def tile(self, layer_id: str, z: int, x: int, y: int) -> Path | None:
        row = self._layers.get(layer_id)
        if not row or not self.covers(layer_id, z, x, y):
            return None
        if self.fake:
            from .simmaps import fake_tile
            path = self.cache.path(f"{row['source']}/{row['id']}", z, x, y)
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(await asyncio.to_thread(fake_tile, row, z, x, y))
            return path
        for i, url in enumerate(self.tile_urls(row, z, x, y)):
            # Each template has its own folder: Kapsi's copy never stands in for the Land Survey's.
            folder = f"{row['source']}/{row['id']}" + (f"-{i}" if i else "")
            if row.get("daily"):
                folder += f"/{daily_date()}"  # a day's picture never stands in for another day's
                # "No picture yet" is only believed for an hour: the day's picture arrives late.
                empty = self.cache.path(folder, z, x, y)
                try:
                    st = empty.stat()
                    if not st.st_size and time.time() - st.st_mtime > 3600:
                        empty.unlink()
                except FileNotFoundError:
                    pass
            path = await self.cache.get(folder, z, x, y, url)
            if path:
                return path
            if row["source"] not in self.cache.failing and not row.get("daily"):
                return None  # answered, just nothing here (a daily picture: try the next satellite)
        return None

    def prune(self) -> int:
        return self.cache.prune()
