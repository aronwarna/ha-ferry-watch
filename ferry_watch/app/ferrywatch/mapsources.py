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
  answer, so a service whose list can't be read still works. `format` is what to ask
  for (GeoServer's image/vnd.jpeg-png keeps tiles small and see-through at the edges),
  `opaque` marks a service that answers white outside its pictures, and `crs`
  EPSG:3301 one that draws only in Estonia's grid (the NAS warps its pictures).
- "xyz": a plain tile template ({z}/{x}/{y}, {key} for the Land Survey's API key).
  A list of templates is tried in order (the first that answers wins).
- "arcgis": an ArcGIS MapServer, drawn through its export call.
- "rest": a folder of ArcGIS MapServers, one picture each (Kirkkonummi's old photos and
  maps). The folder's list is read like a WMS's GetCapabilities, the source's rules pick
  the services, and each is drawn through its export call in web mercator.

An "xyz" source with `years` and `"wide": False` is one place's old photo as plain tiles
(Sipoo 1987); its coverage is worked out from a few tiles stitched together.

Layers come in three groups, which decide where the panel shows them:
"photo" (old aerial photos) and "map" (old maps) are the years of the Historic
map type; "overlay" layers are switches in Layers, on top of any map type.

Old photos and maps get two more steps (histclean.py): once per layer, where it really
has picture (its coverage, which the panel uses to offer only the years that show
where you look); and for each tile, the white or black "no picture" areas at its edges
made see-through.

Historical pictures don't change, so tiles are kept for half a year, in their own
folder with their own size limit (they never push chart tiles out).
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import math
import os
import re
import shutil
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any
from urllib.parse import quote

import aiohttp

from PIL import Image

from .config import USER_AGENT
from .histclean import Cover, box_merc, clean_tile, encode, footprint, lest_box_for, overview_size, reproject_lest, tile_merc
from .tilecache import TileCache

_LOGGER = logging.getLogger(__name__)

HALF = 20037508.342789244
TILE_MAX_AGE = 182 * 86400
CACHE_BYTES = 600 * 1024 * 1024
DISCOVER_EVERY = 86400
CAPS_MAX_BYTES = 40 * 1024 * 1024  # a GetCapabilities answer larger than this is cut off
TILE_REV = "h2"        # old photo and map tiles as cleaned by histclean (0.68)
COVER_REV = 1
COVER_RETRY = 6 * 3600  # a layer whose coverage couldn't be worked out is tried again after this

# Year ranges a rule's year must fall in (anything else in a name is not a year).
YEAR_RE = re.compile(r"(?<!\d)(1[5-9]\d\d|20[0-4]\d)(?!\d)")

ESPOO_BOX = (60.04, 24.47, 60.41, 24.91)     # south, west, north, east
HELSINKI_BOX = (60.01, 24.76, 60.32, 25.29)
VANTAA_BOX = (60.21, 24.64, 60.43, 25.36)
FINLAND_BOX = (59.3, 19.0, 70.2, 31.7)
NORTH_BOX = (53.0, 9.0, 71.0, 33.0)          # Finland, the Baltic and its other shores
ESTONIA_BOX = (57.5, 21.7, 59.75, 28.3)

GEOSERVER = "image/vnd.jpeg-png"  # GeoServer: JPEG where the picture is whole, PNG with see-through at its edges


def _series(name: str, label: str, y0: int, y1: int | None, z0: int, z1: int, group: str = "map") -> dict:
    """A rule for one named layer that is a whole map series (several years of sheets)."""
    return {"match": f"^{re.escape(name)}$", "group": group, "label": label, "year": y0, "year_to": y1,
            "min_zoom": z0, "max_zoom": z1}


# fmt: off
SOURCES: list[dict[str, Any]] = [
    {
        # Layer names and areas checked against the service's own list, Nov 2026. It can't draw
        # see-through: outside its pictures it answers white, which histclean removes.
        "id": "espoo", "name": "Espoo", "kind": "wms",
        "url": "https://kartat.espoo.fi/TeklaOgcWeb/WMS.ashx",
        "attribution": "© Espoon kaupunki (CC BY 4.0)",
        "box": ESPOO_BOX, "max_zoom": 19, "format": "image/png", "opaque": True,
        "rules": [
            # Ortokartta_1950 … Ortokartta_2024; plain "Ortokartta" is the newest (2026).
            {"match": r"^Ortokartta_(\d{4})$", "group": "photo"},
            {"match": r"^Ortokartta$", "group": "photo", "year": 2026},
            {"match": r"^Opaskartta_(19\d\d)$", "group": "map", "label": "Guide map of Espoo"},
            {"match": r"^Kartta_1958$", "group": "map", "label": "Map of Espoo"},
        ],
        "known": ["Ortokartta", "Ortokartta_2024", "Ortokartta_2023", "Ortokartta_2021", "Ortokartta_2019",
                  "Ortokartta_2017", "Ortokartta_2015", "Ortokartta_2013", "Ortokartta_2011", "Ortokartta_2009",
                  "Ortokartta_2007", "Ortokartta_2005", "Ortokartta_2001", "Ortokartta_1996", "Ortokartta_1976",
                  "Ortokartta_1969", "Ortokartta_1950", "Opaskartta_1973", "Opaskartta_1976", "Kartta_1958"],
    },
    {
        "id": "helsinki", "name": "Helsinki", "kind": "wms",
        "url": "https://kartta.hel.fi/ws/geoserver/avoindata/wms",
        "attribution": "© Helsingin kaupunki (CC BY 4.0)",
        "box": HELSINKI_BOX, "max_zoom": 19, "format": GEOSERVER,
        "rules": [
            # One picture per year: plain names first, then the coarsest resolution.
            {"match": r"^avoindata:Ortoilmakuva_(\d{4})(?:_(\d+)cm)?$", "group": "photo"},
            {"match": r"^avoindata:Plan_ofver_Helsingfors_stad_i_borjan_af_1800_talet$", "group": "map", "year": 1800,
             "label": "Plan of Helsingfors"},
            {"match": r"^avoindata:(Pitajakuvauskartta_1749|Rekognosointikartta_1780_1782|Asemakaavakartta_\d{4}"
                      r"|Senaatinkartta_1870|Plan_af_Helsingfors_1900|Kartta_Helsingin_kaupungista_ja_sen_alueista_1909"
                      r"|Helsingin_kaupunki_1917_1918|Helsingin_kaupungin_kartta_1925|Helsinki_1940"
                      r"|Helsingin_matkailijakartta_1952|Helsingin_keskustan_kartta_1962|Opaskartta_19\d\d)$", "group": "map"},
        ],
        "known": ["avoindata:Ortoilmakuva_" + y for y in (
                      "1932", "1943", "1950", "1954", "1956", "1961", "1964", "1969", "1972", "1976", "1987",
                      "1988", "1991", "1993", "1995", "1996", "1997", "1998", "2001", "2005", "2010", "2011",
                      "2012", "2013", "2014", "2016", "2018", "2020")]
                 + ["avoindata:" + n for n in (
                      "Pitajakuvauskartta_1749", "Rekognosointikartta_1780_1782", "Asemakaavakartta_1820",
                      "Asemakaavakartta_1838", "Senaatinkartta_1870", "Asemakaavakartta_1878", "Plan_af_Helsingfors_1900",
                      "Kartta_Helsingin_kaupungista_ja_sen_alueista_1909", "Helsingin_kaupunki_1917_1918",
                      "Helsingin_kaupungin_kartta_1925", "Helsinki_1940", "Helsingin_matkailijakartta_1952",
                      "Helsingin_keskustan_kartta_1962", "Opaskartta_1965", "Opaskartta_1971", "Opaskartta_1977",
                      "Opaskartta_1983", "Opaskartta_1989", "Opaskartta_1999")],
    },
    {
        "id": "vantaa", "name": "Vantaa", "kind": "wms",
        "url": "https://gis.vantaa.fi/geoserver/ows",
        "attribution": "© Vantaan kaupunki (CC BY 4.0)",
        "box": VANTAA_BOX, "max_zoom": 19, "format": GEOSERVER,
        "rules": [
            {"match": r"^historia:orto_(\d{4})$", "group": "photo"},
            {"match": r"^historia:kuninkaankartta$", "group": "map", "year": 1776, "year_to": 1805, "label": "King's map"},
            {"match": r"^historia:(?:pitajankartta|peruskartta|osoitekartta|opaskartta|senaattikartta)_?(\d{4})$",
             "group": "map"},
        ],
        "known": ["historia:orto_" + y for y in (
                      "1954", "1976", "1986", "1996", "1998", "2001", "2004", "2007", "2009", "2011", "2013", "2015",
                      "2017", "2019", "2021", "2023")]
                 + ["historia:" + n for n in (
                      "kuninkaankartta", "pitajankartta_1749", "senaattikartta_1872", "pitajankartta_1933",
                      "osoitekartta1958", "peruskartta_1958", "peruskartta_1967", "opaskartta1969", "opaskartta1976",
                      "opaskartta1982", "opaskartta1988", "opaskartta1996", "opaskartta2004")],
    },
    {
        # The Land Survey's old printed map series (1860s on), georeferenced and served by
        # CSC's Paituli: the whole country, so also Kirkkonummi, Porkkala and the archipelago.
        "id": "mml-old", "name": "Land Survey old maps", "kind": "wms",
        "url": "https://paituli.csc.fi/geoserver/paituli/wms",
        "attribution": "Old maps © Maanmittauslaitos (CC BY 4.0), via CSC Paituli",
        "box": FINLAND_BOX, "max_zoom": 17, "format": GEOSERVER,
        "rules": [
            _series("mml_ven_topo_21k_1871_1919", "Senate atlas 1:21 000", 1871, 1919, 9, 17),
            _series("mml_ven_topo_42k_1870_1944", "Russian topographic map 1:42 000", 1870, 1944, 8, 15),
            _series("mml_ven_topo_42k_192x", "Russian topographic map 1:42 000", 1920, 1929, 8, 15),
            _series("mml_ven_topo_84k_1916-1917", "Russian topographic map 1:84 000", 1916, 1917, 7, 14),
            _series("mml_yleiskartta_400k_1863-1879", "General map 1:400 000", 1863, 1879, 5, 11),
            _series("mml_yleiskartta_400k_1880-1899", "General map 1:400 000", 1880, 1899, 5, 11),
            _series("mml_yleiskartta_400k_1900-1919", "General map 1:400 000", 1900, 1919, 5, 11),
            _series("mml_yleiskartta_400k_1920-1939", "General map 1:400 000", 1920, 1939, 5, 11),
            _series("mml_yleiskartta_400k_1940-1972", "General map 1:400 000", 1940, 1972, 5, 11),
            _series("mml_taloudellinen_100k_1911-1970", "Economic map 1:100 000", 1911, 1970, 7, 14),
            _series("mml_pitajakartta_20k_1912-1968", "Parish map 1:20 000", 1912, 1968, 9, 17),
            _series("mml_topo_50k_1922-1946", "Topographic map 1:50 000", 1922, 1946, 8, 15),
            _series("mml_topo_20k_1924-1948", "Topographic map 1:20 000", 1924, 1948, 9, 17),
            _series("mml_topo_100k_1928_1947", "Topographic map 1:100 000", 1928, 1947, 7, 14),
            _series("mml_sverige-finland_100k_1944", "Sverige-Finland map 1:100 000", 1944, None, 7, 14),
            _series("mml_topo_20k_1947-1959", "Basic map 1:20 000", 1947, 1959, 9, 17),
            _series("mml_topo_100k_1948_1989", "Topographic map 1:100 000", 1948, 1989, 7, 14),
            _series("mml_topo_20k_1960-1969", "Basic map 1:20 000", 1960, 1969, 9, 17),
            _series("mml_topo_50k_1964-2006", "Terrain map 1:50 000", 1964, 2006, 8, 15),
            _series("mml_topo_20k_1970-1979", "Basic map 1:20 000", 1970, 1979, 9, 17),
            _series("mml_perus_50k_1972-1991", "Basic map 1:50 000", 1972, 1991, 8, 15),
            _series("mml_topo_20k_1980-1997", "Basic map 1:20 000", 1980, 1997, 9, 17),
            _series("mml_maastor_20k_1993-2006", "Terrain map 1:20 000", 1993, 2006, 9, 17),
            _series("mml_peruskartta_2010", "Basic map 1:20 000", 2010, None, 9, 17),
        ],
        "known": ["mml_ven_topo_21k_1871_1919", "mml_ven_topo_42k_1870_1944", "mml_ven_topo_42k_192x",
                  "mml_ven_topo_84k_1916-1917", "mml_yleiskartta_400k_1863-1879", "mml_yleiskartta_400k_1880-1899",
                  "mml_yleiskartta_400k_1900-1919", "mml_yleiskartta_400k_1920-1939", "mml_yleiskartta_400k_1940-1972",
                  "mml_taloudellinen_100k_1911-1970", "mml_pitajakartta_20k_1912-1968", "mml_topo_50k_1922-1946",
                  "mml_topo_20k_1924-1948", "mml_topo_100k_1928_1947", "mml_sverige-finland_100k_1944",
                  "mml_topo_20k_1947-1959", "mml_topo_100k_1948_1989", "mml_topo_20k_1960-1969", "mml_topo_50k_1964-2006",
                  "mml_topo_20k_1970-1979", "mml_perus_50k_1972-1991", "mml_topo_20k_1980-1997",
                  "mml_maastor_20k_1993-2006", "mml_peruskartta_2010"],
    },
    {
        # The capital region's joint photo flights (HSY): also Kirkkonummi with Porkkala, Kerava,
        # Järvenpää, Tuusula, Nurmijärvi and Sipoo, 2015 onwards.
        "id": "hsy-region", "name": "Helsinki region", "kind": "wms",
        "url": "https://kartta.hsy.fi/geoserver/wms",
        "attribution": "Orthophoto © the region's municipalities, HSY, HSL and the Defence Forces (CC BY 4.0)",
        "box": (59.87, 24.22, 60.61, 25.54), "max_zoom": 19, "format": GEOSERVER,
        "rules": [
            {"match": r"(?i)^taustakartat_ja_aluejaot:ortoilmakuva_?(20\d\d)$", "group": "photo"},
        ],
        "known": ["taustakartat_ja_aluejaot:ortoilmakuva2015", "taustakartat_ja_aluejaot:Ortoilmakuva_2017",
                  "taustakartat_ja_aluejaot:Ortoilmakuva_2019", "taustakartat_ja_aluejaot:Ortoilmakuva_2021",
                  "taustakartat_ja_aluejaot:Ortoilmakuva_2023"],
    },
    {
        # Kirkkonummi's own historical layers (Porkkala included): photo flights from 1944, just
        # before the Soviet lease, and 1954-56, just after, to 2017; and the old map series. An
        # ArcGIS server whose pictures are stored in the Finnish GK25 grid but drawn in web
        # mercator on request. Terms aren't stated; the municipality's own public map uses it.
        # Their mosaics have black or white "no picture" collars, so both colours are cleaned.
        "id": "kirkkonummi", "name": "Kirkkonummi", "kind": "rest",
        "url": "https://paikkatieto.kirkkonummi.fi/server/rest/services", "folder": "Historia_aineisto",
        "attribution": "© Kirkkonummen kunta",
        "box": (59.84, 24.13, 60.36, 24.90), "max_zoom": 18, "opaque": True,
        "rules": [
            {"match": r"(?i)/(ilmakuva|ortokuva)_", "group": "photo", "year_required": True, "min_zoom": 9},
            _series("Historia_aineisto/Kuninkaankartasto_1776", "King's map", 1776, None, 9, 17),
            _series("Historia_aineisto/Pitäjänkartasto_1840", "Parish map atlas", 1840, None, 9, 17),
            _series("Historia_aineisto/Senaatinkartasto_1870", "Senate atlas", 1870, None, 9, 17),
            _series("Historia_aineisto/Pitäjänkartta_1932_1943", "Parish map", 1932, 1943, 9, 17),
            _series("Historia_aineisto/Peruskartta_1960", "Basic map", 1960, None, 9, 17),
        ],
        "known": [f"Historia_aineisto/{n}" for n in (
            "Ilmakuva_1944", "Ilmakuva_1954_1956", "Ilmakuva_1976_1977", "Ilmakuva_1983", "Ilmakuva_1992_1993",
            "Ilmakuva_1998_1999", "Ilmakuva_2005_2006", "ortokuva_2011", "ortokuva_2017", "Kuninkaankartasto_1776",
            "Pitäjänkartasto_1840", "Senaatinkartasto_1870", "Pitäjänkartta_1932_1943", "Peruskartta_1960")],
    },
    {
        # Sipoo's own 1987 photo flight, published as plain web-mercator tiles (ArcGIS Online).
        "id": "sipoo", "name": "Sipoo", "kind": "xyz", "group": "photo", "title": "Aerial photo", "wide": False,
        "years": {1987: "https://tiles-eu1.arcgis.com/1uXGIDAAq2wrQcJ8/arcgis/rest/services/"
                        "SipoonPaikkatietotuki_Ilmakuva_Sipoosta_vuodelta_1987/MapServer/tile/{z}/{y}/{x}"},
        "attribution": "© Sipoon kunta",
        "box": (60.16, 25.03, 60.57, 25.54), "min_zoom": 11, "max_zoom": 19,
    },
    {
        "id": "turku", "name": "Turku", "kind": "wms",
        "url": "https://turku.asiointi.fi/teklaogcweb/wms.ashx",
        "attribution": "© Turun kaupunki (CC BY 4.0)",
        "box": (60.19, 21.50, 60.91, 23.18), "max_zoom": 19, "format": "image/png", "opaque": True,
        "rules": [
            {"match": r"(?i)ilmakuva", "exclude": r"(?i)vaaravari|vääräväri", "group": "photo", "year_required": True},
            {"match": r"(?i)^Turun Osoitekartta (\d{4})$", "group": "map", "label": "Address map of Turku"},
            {"match": r"(?i)^Virastokartta (\d{4})-(\d\d)$", "group": "map", "label": "Office map of Turku"},
        ],
        "known": ["Ilmakuva 2025", "Ilmakuva 2023", "Ilmakuva 2022 True ortho", "Kaarina Paimio Lieto ilmakuva 2022",
                  "Ilmakuva 2021", "Turunseutu ilmakuva 2018", "Turunseutu ilmakuva 2013", "Turku ilmakuva Ruissalo 2011",
                  "Turku ilmakuva 2010", "Piikkio ilmakuva 2008", "Kaarina ilmakuva 2007", "Turku ilmakuva 2005",
                  "Turunseutu ilmakuva 2002", "Turku ilmakuva 1998", "Turku ilmakuva 1986", "Turku ilmakuva 1976",
                  "Turku ilmakuva 1973", "Turku ilmakuva 1967", "Turku ilmakuva 1958", "Turku ilmakuva 1956",
                  "Turku ilmakuva 1948", "Turku ilmakuva 1939", "Turun Osoitekartta 1945", "Turun Osoitekartta 1950",
                  "Turun Osoitekartta 1954", "Turun Osoitekartta 1958", "Turun Osoitekartta 1962",
                  "Turun Osoitekartta 1968", "Turun Osoitekartta 1972", "Turun Osoitekartta 1979",
                  "Turun Osoitekartta 1984", "Virastokartta 1950-54", "Virastokartta 1958-61", "Virastokartta 1980-89"],
    },
    {
        "id": "oulu", "name": "Oulu", "kind": "wms",
        "url": "https://e-kartta.ouka.fi/TeklaOgcWebOpen/WMS.ashx",
        "attribution": "© Oulun kaupunki (CC BY 4.0)",
        "box": (64.80, 24.90, 65.60, 26.80), "max_zoom": 19, "format": "image/png", "opaque": True,
        "rules": [
            {"match": r"(?i)ilmakuva.*\d{4}|orto.*\d{4}", "exclude": r"(?i)vaara|vääräv|cir", "group": "photo",
             "title_too": True, "year_required": True},
        ],
        "known": ["Ilmakuva 2024", "Ilmakuva 2018 re", "Ilmakuva 2012 Oulu"],
    },
    {
        # Estonia's Land Board: every national photo flight since 1993 and old maps back to
        # the 1860s. The service draws only in Estonia's own grid (EPSG:3301), so the NAS
        # warps each picture onto the web map (histclean.reproject_lest).
        "id": "estonia-old", "name": "Estonia", "kind": "wms", "crs": "EPSG:3301",
        "url": "https://kaart.maaamet.ee/wms/ajalooline",
        "attribution": "© Maa- ja Ruumiamet (Estonian Land and Spatial Development Board)",
        "box": ESTONIA_BOX, "max_zoom": 18, "format": "image/png", "opaque": True,
        "rules": [
            _series("of1993-2000_10k", "Aerial photo", 1993, 2000, 9, 17, "photo"),
            {"match": r"^of(2002|2005)$", "group": "photo", "min_zoom": 9},
            {"match": r"^of(20\d\d)aero$", "group": "photo", "min_zoom": 9},
            _series("kolmeverstakaart", "Three-verst map 1:126 000", 1866, 1915, 7, 13),
            _series("yheverstakaart", "One-verst map 1:42 000", 1894, 1922, 8, 15),
            _series("kaheverstakaart", "Two-verst map 1:84 000", 1895, 1918, 7, 14),
            _series("ew_25T", "Estonian topographic map 1:25 000", 1923, 1935, 9, 16),
            _series("ew_50T", "Estonian topographic map 1:50 000", 1935, 1939, 8, 15),
            _series("kk1940", "Cadastral map", 1930, 1944, 10, 17),
            _series("heereskarte", "German army map 1:300 000", 1943, 1944, 6, 12),
            _series("nltopo_vanem_25T", "Soviet topographic map 1:25 000 (early)", 1946, 1959, 9, 16),
            _series("nltopo_keskmine_25T", "Soviet topographic map 1:25 000 (middle)", 1960, 1974, 9, 16),
            _series("nltopo_uuem_25T", "Soviet topographic map 1:25 000 (late)", 1975, 1989, 9, 16),
            _series("nl_merekaart", "Soviet sea chart 1:50 000", 1981, 1991, 8, 15),
            _series("pohi_vr", "Basic map 1:20 000", 1996, 2007, 9, 17),
        ],
        "known": ["of1993-2000_10k", "of2002", "of2005"] + [f"of{y}aero" for y in range(2007, 2026)]
                 + ["kolmeverstakaart", "yheverstakaart", "kaheverstakaart", "ew_25T", "ew_50T", "kk1940", "heereskarte",
                    "nltopo_vanem_25T", "nltopo_keskmine_25T", "nltopo_uuem_25T", "nl_merekaart", "pohi_vr"],
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
        "id": "porkkala-lease", "name": "Kirkkonummi", "kind": "arcgis", "group": "overlay",
        "title": "Porkkala lease area 1944-56",
        "info": "The border of the area the Soviet Union held as a naval base from 1944 to 1956.",
        "url": "https://paikkatieto.kirkkonummi.fi/server/rest/services/Historia_aineisto/Parenteesialueen_raja/"
               "MapServer/export",
        "attribution": "© Kirkkonummen kunta",
        "box": (59.95, 24.13, 60.25, 24.80), "min_zoom": 8, "max_zoom": 18,
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
    "senaatinkartta": "Senate map", "senaattikartta": "Senate map", "plan af helsingfors": "Plan of Helsingfors",
    "asemakaavakartta": "Town plan map", "kartta helsingin kaupungista": "Map of Helsinki",
    "helsingin kaupunki": "Map of Helsinki", "helsingin kaupungin kartta": "Map of Helsinki", "helsinki 1940": "Map of Helsinki",
    "matkailijakartta": "Tourist map of Helsinki", "keskustan kartta": "Map of central Helsinki",
    "osoitekartta": "Address map", "kuninkaankart": "King's map",
    "opaskartta": "Guide map", "espoon kartta": "Map of Espoo", "senaatin": "Senate map", "pitajankartta": "Parish map",
    "pitajan": "Parish map", "pitäjän": "Parish map", "peruskartta": "Basic map", "yleiskartta": "General map",
    "venal": "Russian topographic map", "venäl": "Russian topographic map", "russian": "Russian topographic map",
    "kalmberg": "Kalmberg's map", "taloudellinen": "Economic map", "topografinen": "Topographic map",
}

# A span of years in a layer's name: "1780_1782", "1912-1968", "1950-54", "192x" (the 1920s).
RANGE_RE = re.compile(r"(?<!\d)(1[5-9]\d\d|20[0-4]\d)\s*[-–_]\s*(1[5-9]\d\d|20[0-4]\d|\d\d)(?!\d|\s*cm)", re.I)
DECADE_RE = re.compile(r"(?<!\d)(1[5-9]\d|20[0-4])x(?![a-z])", re.I)


def _label(name: str, title: str, group: str) -> str:
    if group == "photo":
        return "Aerial photo"
    text = _pretty(name).lower()
    for key, label in MAP_NAMES.items():
        if key in text:
            return label
    text = re.sub(r"\(\s*[-–]?\s*\)", "", YEAR_RE.sub("", title or _pretty(name)))
    return re.sub(r"\s+", " ", text).strip(" -–_,") or "Old map"


def _years(name: str, title: str) -> tuple[int | None, int | None]:
    """The year (or first year) and last year a layer's name or title gives."""
    for text in (name, title):
        m = RANGE_RE.search(text or "")
        if m:
            y0, y1 = int(m.group(1)), m.group(2)
            y1 = int(y1) if len(y1) == 4 else y0 // 100 * 100 + int(y1)
            if y1 > y0:
                return y0, y1
        m = DECADE_RE.search(text or "")
        if m:
            return int(m.group(1)) * 10, int(m.group(1)) * 10 + 9
        years = YEAR_RE.findall(text or "")
        if years:
            return int(years[0]), None
    return None, None


def _base(name: str) -> str:
    """A layer's name without its years and resolution: two layers with the same base and
    year are the same picture at two resolutions."""
    text = RANGE_RE.sub("", name.split(":")[-1].lower())
    text = re.sub(r"_?\d+\s*cm|true ?ortho|tosi", "", YEAR_RE.sub("", text))
    return re.sub(r"[\s_-]+", " ", text).strip()


def layers_from_names(source: dict, found: list[dict]) -> list[dict]:
    """Turn a source's service layers ({name, title, box}) into our layers, by its rules.
    One layer per (group, years, base name) per source: a photo given at several
    resolutions is taken once, at its plainest name or else its coarsest resolution."""
    picked: dict[tuple, tuple] = {}
    for entry in found:
        name, title = entry["name"], entry.get("title") or ""
        for rule in source["rules"]:
            text = f"{name} {title}" if rule.get("title_too") else name
            m = re.search(rule["match"], text)
            if not m:
                continue
            year, year_to = rule.get("year"), rule.get("year_to")
            if year is None:
                year, year_to = _years(name, title if rule.get("title_too") or not YEAR_RE.search(name) else "")
            if rule.get("exclude") and re.search(rule["exclude"], text):
                continue
            if rule.get("year_required") and year is None:
                continue
            if rule.get("need_year") and (year is None or year > 2020):
                continue  # today's maps aren't old maps
            group = rule["group"]
            label = rule.get("label") or _label(name, title, group)
            # Prefer the plain name, then larger cm per pixel (two groups in the photo rule).
            cm = m.group(2) if group == "photo" and m.re.groups >= 2 and m.group(2) and m.group(2).isdigit() else None
            rank = (0 if cm is None else 1, -(int(cm) if cm else 0))
            key = (group, year, year_to, _base(name) if group == "photo" else label)
            if key in picked and picked[key][0] <= rank:
                break
            box = entry.get("box") or source["box"]
            picked[key] = (rank, {
                "id": f"{source['id']}-{_slug(name.split(':')[-1].split('/')[-1])}",
                "source": source["id"], "where": source["name"], "group": group, "year": year, "year_to": year_to,
                "label": label, "title": title or _pretty(name), "layer": name,
                "format": rule.get("format", source.get("format", "image/png")), "box": [round(v, 4) for v in box],
                "min_zoom": rule.get("min_zoom", source.get("min_zoom", 8)),
                "max_zoom": rule.get("max_zoom", source.get("max_zoom", 18)),
                "attribution": source["attribution"], "info": rule.get("info", ""),
                "opacity": rule.get("opacity", 1),
            })
            break
    # Rules that want only the newest year (e.g. yearly traffic maps) keep one layer.
    for rule in source["rules"]:
        if rule.get("latest_only"):
            same = [k for k in picked if k[0] == rule["group"] and k[3] == (rule.get("label") or "")]
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
            "min_zoom": source.get("min_zoom", 0), "max_zoom": source.get("max_zoom", 18),
            "wide": source.get("wide", True), "year_to": None,
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
        # Where each old photo or map really has picture (histclean.Cover), worked out once.
        self.cover: dict[str, Cover] = {}
        self.cover_failed: dict[str, float] = {}
        self._cover_pending: dict[str, asyncio.Future] = {}
        self._cover_limit = asyncio.Semaphore(2)
        self.covers_rev = 0
        self._load_covers()
        self._rebuild()

    # --- the list ---------------------------------------------------------------------
    def _rebuild(self) -> None:
        layers: dict[str, dict] = {}
        for s in SOURCES:
            if s["kind"] in ("wms", "rest"):
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
        # A layer whose area changed in the service's list needs its coverage again.
        for lid, cover in list(self.cover.items()):
            row = layers.get(lid)
            if row and any(abs(a - b) > 1 for a, b in zip(box_merc(row["box"]), cover.mbox)):
                del self.cover[lid]

    def row(self, layer_id: str) -> dict | None:
        return self._layers.get(layer_id)

    def listing(self) -> dict:
        key = bool(self.store.kv_get("mml_api_key", ""))
        rows = []
        pending = 0
        for row in self._layers.values():
            src = _source(row["source"])
            if src.get("needs_key") and not key and not self.fake:
                continue
            out = {**row, "daily": daily_date()} if row.get("daily") else dict(row)
            if _needs_cover(row):
                out["cover"] = row["id"] in self.cover
                pending += not out["cover"] and row["id"] not in self.cover_failed
            rows.append(out)
        return {"layers": rows, "sources": self.sources(), "covers_rev": self.covers_rev, "covers_pending": pending}

    def covers_payload(self) -> dict:
        """Every known coverage grid: {layer id: [width, height, run lengths]} (cells over
        the layer's box in web mercator, row 0 north)."""
        return {"rev": self.covers_rev,
                "covers": {lid: [c.w, c.h, c.rle()] for lid, c in self.cover.items() if lid in self._layers}}

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
        """Read each WMS's layer list at start and once a day, then work out where each
        old photo and map has picture (once per layer)."""
        await asyncio.to_thread(self._drop_old_tiles)
        while True:
            for s in SOURCES:
                if s["kind"] in ("wms", "rest"):
                    await self.discover(s)
                elif s["kind"] == "wayback":
                    await self.discover_wayback(s)
            self._rebuild()
            await self.find_covers()
            await asyncio.sleep(DISCOVER_EVERY)

    def _drop_old_tiles(self) -> None:
        """Tiles kept before 0.68 were stored as the services sent them (white boxes, black
        borders): they go, once."""
        tiles = self.dir / "tiles"
        mark = tiles / f".{TILE_REV}"
        if mark.exists():
            return
        if tiles.exists():
            shutil.rmtree(tiles, ignore_errors=True)
        tiles.mkdir(parents=True, exist_ok=True)
        mark.touch()

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
            elif s["kind"] == "rest":
                found = await self._rest_list(s)
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
            if s.get("known"):
                return  # keep offering the layers we know (a changed list shouldn't empty the year bar)
        self.found[s["id"]] = [{k: v for k, v in e.items() if k in ("name", "title", "box")}
                               for e in found if layers_from_names(s, [e])]
        self.store.kv_set("mapsources_found", self.found)
        self.checked[s["id"]] = {"ts": time.time(), "error": None}

    async def _rest_list(self, s: dict) -> list[dict]:
        """An ArcGIS folder's map services, as [{name: "Folder/Service"}]."""
        url = f"{s['url']}/{quote(s['folder'])}?f=json"
        async with self.session.get(url, headers={"User-Agent": USER_AGENT},
                                    timeout=aiohttp.ClientTimeout(total=60)) as resp:
            resp.raise_for_status()
            data = await resp.json(content_type=None)
        if "services" not in data:
            raise ValueError(str(data.get("error") or "no service list")[:200])
        return [{"name": e["name"]} for e in data["services"] if e.get("type") == "MapServer"]

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

    # --- where each layer has picture -------------------------------------------------
    def _covers_path(self) -> Path:
        return self.dir / "covers.json"

    def _load_covers(self) -> None:
        try:
            data = json.loads(self._covers_path().read_text())
        except (OSError, ValueError):
            return
        if data.get("rev") != COVER_REV:
            return
        for lid, (box, w, h, rle) in (data.get("covers") or {}).items():
            try:
                self.cover[lid] = Cover.from_rle(box_merc(box), w, h, rle)
            except (TypeError, ValueError):
                continue
        self.covers_rev = len(self.cover)

    def _save_covers(self) -> None:
        rows = {lid: [self._layers[lid]["box"] if lid in self._layers else None, c.w, c.h, c.rle()]
                for lid, c in self.cover.items()}
        data = {"rev": COVER_REV, "covers": {k: v for k, v in rows.items() if v[0]}}
        tmp = self._covers_path().with_suffix(".tmp")
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(data, separators=(",", ":")))
            os.replace(tmp, self._covers_path())
        except OSError as err:
            _LOGGER.warning("Couldn't save the map coverage: %s", err)

    async def find_covers(self) -> None:
        """Work out the coverage of every old photo and map that hasn't got one yet, one
        layer at a time (a few seconds each, once per layer)."""
        todo = [r for r in self._layers.values() if _needs_cover(r) and r["id"] not in self.cover
                and time.time() - self.cover_failed.get(r["id"], 0) > COVER_RETRY]
        if todo:
            _LOGGER.info("Map coverage: working out %s old photos and maps", len(todo))
        for row in todo:
            await self.ensure_cover(row)
            await asyncio.sleep(0.2)

    async def ensure_cover(self, row: dict) -> Cover | None:
        """The layer's coverage, worked out now if not yet known (once at a time per layer)."""
        lid = row["id"]
        if lid in self.cover or not _needs_cover(row):
            return self.cover.get(lid)
        if time.time() - self.cover_failed.get(lid, 0) < COVER_RETRY:
            return None
        if lid in self._cover_pending:
            return await asyncio.shield(self._cover_pending[lid])
        future = asyncio.get_running_loop().create_future()
        self._cover_pending[lid] = future
        cover = None
        try:
            async with self._cover_limit:
                cover = await self._make_cover(row)
        except Exception as err:  # noqa: BLE001 - a layer without coverage still shows (by its box)
            _LOGGER.warning("Map coverage of %s %s (%s): %s", row["where"], row["year"], row.get("layer", row["title"]), err)
            self.cover_failed[lid] = time.time()
        finally:
            del self._cover_pending[lid]
            future.set_result(cover)
        if cover is not None:
            self.cover[lid] = cover
            self.cover_failed.pop(lid, None)
            self.covers_rev += 1
            self._save_covers()
        return cover

    async def _make_cover(self, row: dict) -> Cover:
        src = _source(row["source"])
        mbox = box_merc(row["box"])
        size = overview_size(mbox)
        img = await self._render(row, src, mbox, size, overview=True)
        mask = await asyncio.to_thread(footprint, img, bool(src.get("opaque")))
        cover = Cover.from_mask(mask, mbox)
        _LOGGER.info("Map coverage of %s %s %s: %.0f%% of its box", row["where"], row["year"] or "", row["label"],
                     cover.share() * 100)
        return cover

    async def _render(self, row: dict, src: dict, mbox, size: tuple[int, int], overview: bool = False) -> Image.Image:
        """A picture of the layer over a web-mercator box (for coverage)."""
        if self.fake:
            from .simmaps import fake_render
            return await asyncio.to_thread(fake_render, row, src, mbox, size)
        if src.get("crs") == "EPSG:3301":
            lbox = lest_box_for(mbox)
            lsize = overview_size(lbox, max(size))
            body = await self._get(self._wms_url(src, row, lbox, lsize, "EPSG:3301"), 120)
            return await asyncio.to_thread(lambda: reproject_lest(Image.open(io.BytesIO(body)), lbox, mbox, size))
        if src["kind"] == "xyz":
            return await self._stitch(row, mbox, size)
        url = self._rest_url(src, row, mbox, size) if src["kind"] == "rest" else self._wms_url(src, row, mbox, size)
        body = await self._get(url, 120)
        return await asyncio.to_thread(lambda: Image.open(io.BytesIO(body)).convert("RGBA"))

    async def _stitch(self, row: dict, mbox, size: tuple[int, int], most: int = 16) -> Image.Image:
        """A plain-tile layer over a web-mercator box: the tiles of the deepest zoom that needs
        at most ``most`` of them (from the layer's first zoom), pasted and scaled. Missing
        tiles stay see-through."""
        x0, y0, x1, y1 = mbox
        z = row["min_zoom"]
        def span(zz: int) -> tuple[int, int, int, int]:
            n, w = 2**zz, 2 * HALF / 2**zz
            return (int((x0 + HALF) // w), int((HALF - y1) // w), min(n - 1, int((x1 + HALF) // w)),
                    min(n - 1, int((HALF - y0) // w)))
        while z < row["max_zoom"]:
            a, b, c, d = span(z + 1)
            if (c - a + 1) * (d - b + 1) > most:
                break
            z += 1
        a, b, c, d = span(z)
        sheet = Image.new("RGBA", ((c - a + 1) * 256, (d - b + 1) * 256), (0, 0, 0, 0))
        for tx in range(a, c + 1):
            for ty in range(b, d + 1):
                try:
                    body = await self._get(self.tile_urls(row, z, tx, ty)[0], 60)
                except (aiohttp.ClientResponseError, ValueError):
                    continue  # no tile here: nothing to paste
                sheet.paste(Image.open(io.BytesIO(body)).convert("RGBA"), ((tx - a) * 256, (ty - b) * 256))
        w = 2 * HALF / 2**z
        left, top = (x0 + HALF) / w - a, (HALF - y1) / w - b
        right, bottom = (x1 + HALF) / w - a, (HALF - y0) / w - b
        crop = sheet.crop((round(left * 256), round(top * 256), round(right * 256), round(bottom * 256)))
        return crop.resize(size, Image.BILINEAR)

    async def _get(self, url: str, timeout: float) -> bytes:
        async with self.session.get(url, headers={"User-Agent": USER_AGENT},
                                    timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
            resp.raise_for_status()
            if not resp.content_type.startswith("image/"):
                text = (await resp.text())[:200]
                raise ValueError(f"not a picture ({resp.content_type}): {text}")
            return await resp.read()

    # --- tiles ------------------------------------------------------------------------
    def covers(self, layer_id: str, z: int, x: int, y: int) -> bool | None:
        """True if the layer may have a picture here, False if never (outside its area,
        zooms or real coverage), None if there's no such layer."""
        row = self._layers.get(layer_id)
        if not row:
            return None
        if not (row["min_zoom"] <= z <= row["max_zoom"] and 0 <= x < 2**z and 0 <= y < 2**z):
            return False
        if not overlaps(tile_box(z, x, y), tuple(row["box"])):
            return False
        cover = self.cover.get(layer_id)
        if cover is not None and not cover.any_in(tile_merc(z, x, y), margin=1):
            return False
        return True

    def _wms_url(self, src: dict, row: dict, bbox, size: tuple[int, int], srs: str = "EPSG:3857") -> str:
        fmt = row.get("format") or src.get("format", "image/png")
        return (f"{src['url']}?SERVICE=WMS&VERSION=1.1.1&REQUEST=GetMap&LAYERS={quote(row['layer'], safe=':,')}"
                f"&STYLES=&SRS={srs}&BBOX={','.join(f'{v:.3f}' for v in bbox)}&WIDTH={size[0]}&HEIGHT={size[1]}"
                f"&FORMAT={quote(fmt, safe='/')}&TRANSPARENT=TRUE")

    def _rest_url(self, src: dict, row: dict, bbox, size: tuple[int, int]) -> str:
        return (f"{src['url']}/{quote(row['layer'], safe='/')}/MapServer/export?"
                f"bbox={','.join(f'{v:.3f}' for v in bbox)}&bboxSR=3857&imageSR=3857&size={size[0]},{size[1]}"
                f"&format=png32&transparent=true&f=image")

    def tile_urls(self, row: dict, z: int, x: int, y: int) -> list[str]:
        src = _source(row["source"])
        if src["kind"] == "rest":
            return [self._rest_url(src, row, mercator_bbox(z, x, y), (256, 256))]
        if src["kind"] == "wms":
            if src.get("crs") == "EPSG:3301":
                lbox = lest_box_for(tile_merc(z, x, y))
                return [self._wms_url(src, row, lbox, overview_size(lbox, 288), "EPSG:3301")]
            return [self._wms_url(src, row, mercator_bbox(z, x, y), (256, 256))]
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

    def _processor(self, row: dict, src: dict, z: int, x: int, y: int, cover: Cover | None):
        """What's done to an old photo or map tile before it's kept: warped (Estonia),
        then its "no picture" areas made see-through, then packed small."""
        mb = tile_merc(z, x, y)
        zone = cover.edge_zone(mb) if cover is not None else None
        everywhere = cover is None  # coverage unknown: clean the whole tile
        opaque = bool(src.get("opaque"))
        photo = row["group"] == "photo"
        lbox = lest_box_for(mb) if src.get("crs") == "EPSG:3301" else None

        def run(body: bytes) -> bytes:
            img = Image.open(io.BytesIO(body))
            img = reproject_lest(img, lbox, mb, (256, 256)) if lbox else img.convert("RGBA")
            if zone is not None or everywhere:
                img = clean_tile(img, zone, opaque)
            return b"" if img is None else encode(img, photo)
        return run

    async def tile(self, layer_id: str, z: int, x: int, y: int) -> Path | None:
        row = self._layers.get(layer_id)
        if not row or not self.covers(layer_id, z, x, y):
            return None
        src = _source(row["source"])
        if _needs_cover(row):
            cover = await self.ensure_cover(row)
            if cover is not None and not cover.any_in(tile_merc(z, x, y), margin=1):
                return None
            folder = f"{row['source']}/{TILE_REV}/{row['id']}"
            process = self._processor(row, src, z, x, y, cover)
            if self.fake:
                return await self._fake_tile(folder, row, src, z, x, y, process)
            return await self.cache.get(folder, z, x, y, self.tile_urls(row, z, x, y)[0], transform=process)
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

    async def _fake_tile(self, folder, row, src, z, x, y, process) -> Path | None:
        """Simulator: the made-up service's picture, through the same cleaning as real ones."""
        from .simmaps import fake_render
        path = self.cache.path(folder, z, x, y)
        if not path.exists():
            def make() -> bytes:
                img = fake_render(row, src, tile_merc(z, x, y), (256, 256))
                buf = io.BytesIO()
                img.save(buf, "PNG")
                return buf.getvalue()
            # Estonia's warp is skipped here: the made-up picture is drawn in mercator already.
            body = await asyncio.to_thread(make)
            out = await asyncio.to_thread(self._processor({**row}, {**src, "crs": None}, z, x, y, self.cover.get(row["id"])),
                                          body)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(out)
        return path if path.stat().st_size else None

    def prune(self) -> int:
        return self.cache.prune()


def _source(source_id: str) -> dict:
    return next(s for s in SOURCES if s["id"] == source_id)


def _needs_cover(row: dict) -> bool:
    """Old photos and maps have edges worth finding; the satellite archives cover everything."""
    return (row["group"] in ("photo", "map") and not row.get("wide")
            and _source(row["source"])["kind"] in ("wms", "rest", "xyz"))
