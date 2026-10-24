"""A seamless nautical chart drawn from Finland's open chart data.

Traficom's chart service is scanned paper chart sheets, so sheet edges and inset
boxes show where they meet. This draws the chart itself instead, the way apps
like Luotsi do, from:

- Traficom's depth data (depth areas, contours, soundings) and routeing (traffic
  separation schemes, precautionary areas, dredged and unsurveyed areas),
- Traficom's open data (shore structures, pontoons, pilot boarding places, VTS limits),
- Väylävirasto's fairway data (fairway lines and areas, leading lines, buoys,
  beacons, lights, restricted areas and water traffic signs),
- the National Land Survey's rocks, wrecks, place names and land detail (height
  contours, lakes, roads, railways, buildings, masts and chimneys; with an API key), and
- Museovirasto's register of wrecks, underwater heritage sites and coastal defences.

Data is fetched lazily in blocks (zoom-11 map tiles, about 10 km across) the
first time part of the map is viewed, kept on the NAS for a month, and cut into
small map tiles on request. Land is whatever isn't water: the panel paints the
Finnish area in the land colour and draws the depth areas over it, so islands
line up exactly with the depths around them. Zoomed out (zooms 8-9) tiles are cut
from a coarse copy at the head of each block file on disk, and kept on disk too.

Which layers each service has is read from its capabilities once a day, so a layer
a service doesn't have (or names differently) is left out instead of failing.

RAM is the scarce resource on a small NAS, so blocks are stored and kept in memory
as flat int32 arrays (about 8 bytes a vertex, where parsed JSON takes about 150),
each block file holds levels of detail so zoomed-out tiles only read a simplified
copy, and downloads are parsed a page at a time.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import math
import os
import re
import sys
import time
from array import array
from collections import OrderedDict
from functools import lru_cache
from itertools import chain
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import aiohttp

from .charts import FINNISH_WATERS
from .config import USER_AGENT

_LOGGER = logging.getLogger(__name__)

TRAFICOM_WFS = "https://julkinen.traficom.fi/inspirepalvelu/rajoitettu/wfs"
TRAFICOM_OPEN_WFS = "https://julkinen.traficom.fi/inspirepalvelu/avoin/wfs"
VAYLA_WFS = "https://avoinapi.vaylapilvi.fi/vaylatiedot/vesivaylatiedot/wfs"
# National Land Survey topographic database (rocks, wrecks, names); needs a free API key.
MML_API = "https://avoin-paikkatieto.maanmittauslaitos.fi/maastotiedot/features/v1"
# Finnish Heritage Agency's registers (CC BY 4.0, no key).
MV_WFS = "https://geoserver.museovirasto.fi/geoserver/rajapinta/wfs"
ATTRIBUTION = ("Chart data: Traficom (depths, routeing, harbour structures), Väylävirasto (fairways, aids, signs), "
               "Maanmittauslaitos (rocks, names, land), Museovirasto (wrecks, heritage sites and defences, "
               "CC BY 4.0). Outside Finland: depths EMODnet Bathymetry (CC BY 4.0); coast, sea marks, routeing "
               "and names © OpenStreetMap contributors (ODbL), OpenSeaMap. Not for navigation.")
# Type names by prefix: anything without one is a Land Survey collection.
SERVICES = {"rajoitettu": TRAFICOM_WFS, "avoin": TRAFICOM_OPEN_WFS, "vesivaylatiedot": VAYLA_WFS}

BLOCK_Z = 11  # blocks are zoom-11 map tiles, about 10 km across
BLOCK_MIN_Z = 10  # tiles from this zoom are cut from blocks' levels, below it from their overview levels
MIN_Z = 8  # below this the panel shows Traficom's overview chart
UNITS = 65536  # block coordinates: 0..UNITS across a block, y down
EXTENT = 512  # tile coordinates sent to the panel: 0..EXTENT
TILE_BUFFER = 24  # tile units drawn past each edge, so lines and areas meet seamlessly
POINT_BUFFER = 64  # wider for points: symbols and their text reach past the tile edge
POINT_BUFFERS = {"sector": 320, "aid": 128, "light": 128, "name": 256, "sounding": 128, "landmark": 128,
                 "sign": 96, "call": 128, "building": 16}
BLOCK_POINT_MARGIN = UNITS // 4  # points are kept this far past each block edge...
POINT_MARGINS = {"building": UNITS // 32}  # ...but buildings are dense and small: no need to fetch that much more
MAX_AGE = 30 * 86400
FAILED_RETRY = 3600
SERVICE_DOWN_FOR = 900  # s a service that timed out is left alone
GET_RETRIES = (3, 15)  # s to wait before asking again after a dropped connection
CAPS_MAX_AGE = 86400  # s between reads of which layers each service has
PAGE = 2000  # features per request; a page over MAX_RESPONSE is asked for again in halves
MML_PAGE = 1000
MML_PAGES = {"height": 250, "building": 500, "road": 500, "lake": 250}  # long or dense features: smaller pages
# Bytes; a bigger page is asked for again in halves. Parsing a page is the biggest use of RAM
# here, about seven times its size (a 4 MB page of depth areas takes some 30 MB for a moment).
MAX_RESPONSE = 4 * 1024 * 1024
# Parsed blocks kept in RAM, by their array sizes (blocks with land detail take a few MB
# each at full detail, so the cache holds a handful of them)...
MEMORY_BYTES = 16 * 1024 * 1024
MEMORY_BLOCKS = 64  # ...and by count; the rest are read from disk when needed
FORGET_AFTER = 20  # s; a queued block nobody has asked for again since is skipped
# s without a tile request after which what's in RAM is let go: the chart is mostly not
# being looked at, and on a small NAS those MB are better left to the rest of the system.
IDLE_FORGET = 300
# s an overview tile is shown as cut although blocks in it changed since: while blocks are
# being fetched one after another, a zoom-8 tile isn't cut again from 64 blocks for each.
OVERVIEW_RECUT = 60
TILE_REV = 10  # bump when tiles change, so browsers don't keep drawing cached old ones
HALF = 20037508.342789244  # half the web-mercator world, in metres

# Levels of detail in each block file: (highest zoom it serves, simplification in block
# units, smallest feature kept in block units). Zoomed-out tiles read only the first.
LEVELS = ((11, 24, 64), (13, 8, 16), (99, 0, 0))
# A coarser copy of what overview charts show, ahead of LEVELS in each file, from which the
# zoom-8 and 9 tiles are cut (192 block units is about 60 m on the ground).
OVERVIEW_LEVEL = (9, 192, 512)
# Land Survey data is digitised to a metre or so; simplifying the full detail by about half a
# metre is invisible on the chart and halves the size of contours and roads.
FULL_TOL = {"height": 6, "road": 4, "rail": 4, "lake": 4, "buildarea": 2}

# What to fetch: layer key -> (service, type names, kind, properties to keep). A layer drawn
# from several types (the parts of a traffic separation scheme, kinds of landmark) maps a
# short tag for each to its type names, and the tag is kept as the "_src" property. Every
# name the service has is fetched, so a misspelt duplicate (Traficom has some) or a second
# guess at a Land Survey collection id costs nothing when it doesn't exist, and features
# found under two names are kept once.
SOURCES: dict[str, tuple[str, str | dict[str, tuple[str, ...]], str, tuple[str, ...]]] = {
    "depth": (TRAFICOM_WFS, "rajoitettu:DepthArea_A", "area", ("drval1", "drval2")),
    "contour": (TRAFICOM_WFS, "rajoitettu:DepthContour_L", "line", ("valdco",)),
    "sounding": (TRAFICOM_WFS, "rajoitettu:Sounding_P", "point", ("depth",)),
    "farea": (VAYLA_WFS, "vesivaylatiedot:vaylaalueet_uusi", "area", ("tyyppikoodi", "mitoitussyvays", "jnro")),
    "fline": (VAYLA_WFS, "vesivaylatiedot:navigointilinjat_uusi", "line", ("mitoitussyvays", "jnro")),
    "lead": (VAYLA_WFS, "vesivaylatiedot:taululinjat_uusi", "line", ("suunta",)),
    "route": (VAYLA_WFS, "vesivaylatiedot:vaylat_uusi", "line", ("vaylaluokkakoodi", "jnro")),
    "restrict": (VAYLA_WFS, "vesivaylatiedot:rajoitusalue_a_uusi", "area", ("rajoitustyypit", "suuruus")),
    "aid": (VAYLA_WFS, "vesivaylatiedot:turvalaitteet_uusi", "point",
            ("turvalaitetyyppifi", "navigointilajikoodi", "valaistu", "nimifi")),
    "sector": (VAYLA_WFS, "vesivaylatiedot:valosektorit_uusi", "point", ("alkukulma", "loppukulma", "vari")),
    "shore": (TRAFICOM_OPEN_WFS, "avoin:ShorelineConstruction_L", "line", ()),
    "light": (VAYLA_WFS, "vesivaylatiedot:loistot_uusi", "point", ("viralvalotunnuse",)),
    "rock": (MML_API, "vesikivi", "point", ("kohdeluokka",)),
    "rockarea": (MML_API, "vesikivikko", "area", ("kohdeluokka",)),
    "wreck": (MML_API, "hylky", "point", ("kohdeluokka",)),
    "name": (MML_API, "paikannimi", "point", ("teksti", "kieli", "kohdeluokka")),
    # Routeing (S-57 names; ORIENT is a lane's direction of traffic).
    "tss": (TRAFICOM_WFS, {
        "lane": ("rajoitettu:TrafficSeparationSchemeLanePart_A",),
        "zone": ("rajoitettu:TrafficSeparationZone_A", "rajoitettu:TrafficSeaparationZone_A"),
        "prec": ("rajoitettu:PrecautionaryArea_A",),
        "deep": ("rajoitettu:DeepWaterRoutePart_A",),
        "twoway": ("rajoitettu:TwoWayRoutePart_A",),
        "dredged": ("rajoitettu:DredgedArea_A",),
        "unsurveyed": ("rajoitettu:UnsurveyedArea_A",),
    }, "area", ("_src", "orient", "drval1")),
    # Lines; areas given here (VTS areas) are kept as their outlines.
    "tssline": (TRAFICOM_WFS, {
        "sep": ("rajoitettu:TrafficSeparationLine_L", "rajoitettu:TrafficSeaparationLine_L"),
        "bnd": ("rajoitettu:TrafficSeparationSchemeBoundary_L",),
        "vts": ("avoin:VTSArea_A",),
        "call": ("avoin:Rdocal_L",),
    }, "line", ("_src",)),
    "pilot": (TRAFICOM_OPEN_WFS, "avoin:PilotBoardingPlace_P", "point", ("objnam",)),
    # Submarine cables and pipelines (the paper chart's magenta wavy and dashed lines).
    "cable": (TRAFICOM_WFS, {
        "cable": ("rajoitettu:CableSubmarine_L", "avoin:CableSubmarine_L"),
        "pipe": ("rajoitettu:PipelineSubmarineOnBelow_L", "avoin:PipelineSubmarineOnBelow_L"),
    }, "line", ("_src",)),
    "call": (TRAFICOM_OPEN_WFS, "avoin:Rdocal_P", "point", ("objnam", "comcha", "orient")),
    "pier": (TRAFICOM_OPEN_WFS, {
        "pontoon": ("avoin:Pontoon_L", "avoin:Pontoon_A"), "berth": ("avoin:Berths_L",),
        "dam": ("avoin:Dam_L", "avoin:Dam_A"), "gate": ("avoin:Gate_L",),
    }, "line", ("_src",)),
    "sign": (VAYLA_WFS, "vesivaylatiedot:vesiliikennemerkit_uusi", "point",
             ("vlmlajityyppi", "vlmtyyppi", "rajoitusarvo", "nimifi")),
    # Land Survey topographic database. Collection ids are guesses matched against the
    # service's own list; see _mml_match.
    "lake": (MML_API, {"lake": ("jarvi",), "river": ("virtavesialue",)}, "area", ("_src",)),
    "height": (MML_API, "korkeuskayra", "line", ("korkeusarvo",)),
    "road": (MML_API, "tieviiva", "line", ("kohdeluokka",)),
    "rail": (MML_API, "rautatie", "line", ("kohdeluokka",)),
    # Buildings as points with a size (see build_into); big ones also as areas, in "buildarea".
    "building": (MML_API, "rakennus", "point", ("kohdeluokka", "_size")),
    "buildarea": (MML_API, {}, "area", ("kohdeluokka",)),
    "landmark": (MML_API, {
        "mast": ("masto",), "chimney": ("savupiippu",), "wind": ("tuulivoimala",),
        "tower": ("nakotorni", "torni", "vesitorni"), "church": ("kirkko",),
    }, "point", ("_src", "korkeus", "korkeusarvo", "teksti")),
    # Heights printed beside masts and chimneys, where the database keeps them apart; joined
    # to the landmark they belong to (join_heights) and not kept.
    "markh": (MML_API, {"mast": ("mastonkorkeus",), "chimney": ("savupiipunkorkeus",)}, "point",
              ("_src", "korkeusarvo", "teksti")),
}
DERIVED = {"buildarea": "building"}  # layers filled while fetching another one

# Each block is stored in parts, one file each, so a part added later (or one that
# needs an API key) is fetched on its own without downloading the rest again. A layer
# in a later part replaces the same layer in an earlier one (older base files still
# carry fairways without their class, used until the "fw" part arrives).
PARTS: dict[str, tuple[str, ...]] = {
    "": ("depth", "contour", "sounding", "aid", "sector"),
    "fw": ("farea", "fline", "lead", "route", "restrict"),
    "more": ("shore", "light"),
    "mml": ("rock", "rockarea", "wreck", "name"),
    "tss": ("tss", "tssline", "pilot", "call", "pier", "sign"),
    "cbl": ("cable",),
    "lake": ("lake",),
    "land": ("height", "road", "rail"),
    "mark": ("building", "buildarea", "landmark", "markh"),
    # Outside Finnish waters (see foreign.py): the same layers from open European data, merged
    # into the Finnish ones when read rather than replacing them; "xland" is the land under them.
    "fx": ("depth", "contour", "aid", "sector", "light", "rock", "wreck", "name", "tss", "tssline", "lead",
           "pilot", "xland"),
}
FOREIGN_PART = "fx"
# A part's file is fetched again when its revision here is newer than the one it was written
# with (tss 1: dredged areas' least depth, and dredged and unsurveyed areas in the overview).
PART_REV = {"tss": 1}

MML_PARTS = ("mml", "lake", "land", "mark")  # only with a Land Survey key
# Parts only needed from some zoom: the dense land detail isn't downloaded for blocks only
# ever seen zoomed out.
PART_MIN_Z = {"land": 12, "mark": 12}

# Navigation aid types, as one letter for the panel.
AID_TYPES = {
    "viitta": "v", "poiju": "p", "reunamerkki": "r", "linjamerkki": "l", "kummeli": "k",
    "sektoriloisto": "s", "merimajakka": "m", "tunnusmajakka": "m", "suuntaloisto": "d",
    "apuloisto": "a", "tutkamerkki": "t", "tutkamajakka": "t",
}
MAJOR_AIDS = ("m", "s")  # all that is drawn zoomed out, as on overview charts
# Light sector colours as chart letters, in the order charts print them.
LIGHT_COLOURS = {"v": "W", "p": "R", "vi": "G", "k": "Y"}

# MML feature classes. Rocks: 1 underwater, 2 awash, 3 above water.
ROCK_TYPES = {38511: 1, 38512: 2, 38513: 3}
# Wrecks: 1 depth unknown (dangerous), 2 depth known, 3 hull showing.
WRECK_TYPES = {16721: 1, 16722: 2, 16712: 3}
# Depth contours as the paper chart shows them; Traficom's data has more.
CONTOURS = (3, 6, 10, 20, 50, 100, 200)
OVERVIEW_CONTOURS = (20, 50, 100, 200)
OVERVIEW_TSS = ("lane", "zone", "prec", "deep", "twoway")  # routeing drawn on overview charts
# Dredged and unsurveyed areas aren't depth areas in S-57, so the depth areas have holes
# there. They are painted as water (see _format), from the overview zooms on.
WATER_TSS = ("dredged", "unsurveyed")
WATER_TSS_LIMITS_Z = 10  # their limits (dashed outline, hatching) only from this zoom
OVERVIEW_TSSLINE = ("sep", "bnd")
OVERVIEW_FAIRWAYS = (1, 2)  # merchant fairways (VL1-VL2)

# MML road classes: 1 major (classes I-II), 2 minor (III and driveways), 3 track or path. Ferry
# lines and winter roads (over the ice) are left out. Unknown classes count as minor.
ROAD_CLASSES = {12111: 1, 12112: 1, 12121: 1, 12122: 1, 12131: 2, 12132: 2, 12141: 2,
                12313: 3, 12314: 3, 12316: 3, 12151: 0, 12152: 0, 12153: 0, 12312: 0}
# MML building classes by their first four digits; 42270 is a church.
BUILDING_KINDS = {4221: "home", 4222: "public", 4223: "home", 4224: "industry", 4225: "public", 4226: "other"}
CHURCH = 42270
SMALL_BUILDING = 300  # m² of footprint: smaller is size 1, bigger size 2...
BUILDING_AREA = 3000  # ...and this big is drawn by its outline (size 3, in "buildarea")
HEIGHT_REACH = UNITS // 64  # block units (about 150 m): a height label further than this from a mast isn't its own
# Water traffic signs (vlmlajityyppi). Only 11 = speed limit is known for sure; the rest are
# also told by the sign's type text and name. Types sent to the panel: speed (value km/h),
# nowake, noanchor, nomoor, nopass (no overtaking or meeting), height (headroom, value m), other.
SIGN_CODES = {11: "speed"}
SIGN_WORDS = (("nopeus", "speed"), ("aallo", "nowake"), ("ankkur", "noanchor"), ("kiinnit", "nomoor"),
              ("ohitta", "nopass"), ("kohtaa", "nopass"), ("alikulku", "height"), ("korkeus", "height"))

# Museovirasto's point layers with underwater sites and wrecks, and the register class
# each one is: 0 protected ancient monument, 1 other heritage site, 2 other site,
# 3 possible ancient monument, 4 find spot, 5 observation.
MV_TYPES = {
    "rajapinta:muinaisjaannos_piste": 0,
    "rajapinta:rajapinta_muu_kulttuuriperintokohde_piste": 1,
    "rajapinta:rajapinta_muu_kohde_piste": 2,
    "rajapinta:rajapinta_mahdollinen_muinaisjaannos_piste": 3,
    "rajapinta:rajapinta_loytopaikka_piste": 4,
    "rajapinta:rajapinta_havaintokohde_piste": 5,
}
MV_FIELDS = ("mjtunnus", "kohdenimi", "tyyppi", "alatyyppi", "ajoitus", "paikannustarkkuus", "vedenalainen")
# Register sub-types in English, matched by the start of each word; the first match wins.
MV_SUBTYPES = (
    ("hylyt (puu)", "wooden wreck"), ("hylyt (metalli)", "metal wreck"), ("hyly", "wreck"),
    ("löytöpaik", "find spot"), ("taisteluhau", "trenches"), ("juoksuhau", "trenches"),
    ("tykkiasem", "gun positions"), ("tykkipatter", "gun batteries"), ("patteri", "gun batteries"),
    ("ampumapes", "firing positions"), ("ampuma-asem", "firing positions"), ("konekivä", "machine-gun nests"),
    ("bunkk", "bunkers"), ("betonilinn", "concrete bunkers"), ("korsu", "dugouts"),
    ("linnoit", "fortifications"), ("varustu", "fortifications"), ("linna", "castle"), ("valli", "ramparts"),
    ("kivirakente", "stone structures"), ("kivilaite", "stone structures"), ("kasarm", "barracks"),
    ("luola", "caves and tunnels"), ("tunnel", "tunnels"), ("panssariest", "tank obstacles"),
    ("este", "obstacles"), ("tähysty", "observation posts"), ("kuop", "pits"), ("kaivann", "pits"),
    ("varikko", "depot"), ("ruutikellar", "powder magazine"), ("kellar", "cellars"),
)
HERITAGE_Z = 7  # heritage sites are few, so they're fetched for whole zoom-7 tiles at once
HERITAGE_REGIONS = 6  # regions kept in RAM

# Zoom from which each layer is drawn.
DEFENCE_MIN_Z = 13  # coastal defence sites on land, only drawn up close
COAST_REACH = UNITS // 12  # block units (about 800 m): land sites further from the sea are left out
LAYER_MIN_Z = {"depth": MIN_Z, "contour": MIN_Z, "sounding": 12, "farea": 11, "fline": MIN_Z,
               "lead": 11, "route": 11, "restrict": 12, "aid": MIN_Z, "sector": 11, "shore": 12, "light": 10,
               "rock": 12, "rockarea": 12, "wreck": 12, "name": MIN_Z, "heritage": 11,
               "tss": MIN_Z, "tssline": MIN_Z, "pilot": 10, "call": 11, "pier": 12, "sign": 13, "cable": 11,
               "lake": 10, "height": 12, "road": 12, "rail": 12, "building": 13, "buildarea": 13, "landmark": 12,
               "xland": MIN_Z}


def source_names(key: str) -> list[tuple[str | None, str]]:
    """(tag, type name) of everything a layer is fetched from; the tag is None for a layer
    with a single source."""
    names = SOURCES[key][1]
    if isinstance(names, str):
        return [(None, names)]
    return [(tag, name) for tag, alts in names.items() for name in alts]


def service_of(name: str) -> str:
    prefix = name.split(":", 1)[0] if ":" in name else ""
    return SERVICES.get(prefix, MML_API)


# --- geometry ------------------------------------------------------------------------

def clip_ring(pts: list[tuple[float, float]], x0: float, y0: float, x1: float, y1: float) -> list:
    """Sutherland-Hodgman: the part of a polygon ring inside the box. Edges along the
    box are fine for filling, which is all rings are used for."""
    for edge in range(4):
        if not pts:
            return pts
        out = []
        px, py = pts[-1]
        for cx, cy in pts:
            if edge == 0:
                cin, pin = cx >= x0, px >= x0
            elif edge == 1:
                cin, pin = cx <= x1, px <= x1
            elif edge == 2:
                cin, pin = cy >= y0, py >= y0
            else:
                cin, pin = cy <= y1, py <= y1
            if cin != pin:
                if edge < 2:
                    xe = x0 if edge == 0 else x1
                    out.append((xe, py + (cy - py) * (xe - px) / (cx - px)))
                else:
                    ye = y0 if edge == 2 else y1
                    out.append((px + (cx - px) * (ye - py) / (cy - py), ye))
            if cin:
                out.append((cx, cy))
            px, py = cx, cy
        pts = out
    return pts


def clip_line(pts: list[tuple[float, float]], x0: float, y0: float, x1: float, y1: float) -> list[list]:
    """Liang-Barsky per segment: the pieces of a line inside the box."""
    parts: list[list] = []
    cur: list = []
    for (ax, ay), (bx, by) in zip(pts, pts[1:]):
        dx, dy = bx - ax, by - ay
        t0, t1 = 0.0, 1.0
        ok = True
        for p, q in ((-dx, ax - x0), (dx, x1 - ax), (-dy, ay - y0), (dy, y1 - ay)):
            if p == 0:
                if q < 0:
                    ok = False
                    break
            else:
                t = q / p
                if p < 0:
                    if t > t1:
                        ok = False
                        break
                    t0 = max(t0, t)
                else:
                    if t < t0:
                        ok = False
                        break
                    t1 = min(t1, t)
        if not ok:
            if len(cur) > 1:
                parts.append(cur)
            cur = []
            continue
        start = (ax + t0 * dx, ay + t0 * dy)
        end = (ax + t1 * dx, ay + t1 * dy)
        if not cur or cur[-1] != start:
            if len(cur) > 1:
                parts.append(cur)
            cur = [start]
        cur.append(end)
        if t1 < 1.0:
            parts.append(cur)
            cur = []
    if len(cur) > 1:
        parts.append(cur)
    return parts


def simplify(pts: list, tol: float) -> list:
    """Douglas-Peucker with a squared tolerance, keeping the end points."""
    n = len(pts)
    if n < 3:
        return pts
    keep = [False] * n
    keep[0] = keep[-1] = True
    tol2 = tol * tol
    stack = [(0, n - 1)]
    while stack:
        a, b = stack.pop()
        ax, ay = pts[a]
        bx, by = pts[b]
        dx, dy = bx - ax, by - ay
        len2 = dx * dx + dy * dy
        best, idx = -1.0, -1
        for i in range(a + 1, b):
            px, py = pts[i]
            if len2 == 0:
                d = (px - ax) ** 2 + (py - ay) ** 2
            else:
                t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / len2))
                d = (px - ax - t * dx) ** 2 + (py - ay - t * dy) ** 2
            if d > best:
                best, idx = d, i
        if best > tol2:
            keep[idx] = True
            stack.append((a, idx))
            stack.append((idx, b))
    return [p for p, k in zip(pts, keep) if k]


def _bbox(pts) -> tuple[float, float, float, float]:
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


def _flat(pts) -> list[int]:
    """[(x, y), ...] -> [x0, y0, x1, y1, ...] as whole tile units, dropping repeats."""
    out: list[int] = []
    for x, y in pts:
        xi, yi = round(x), round(y)
        if len(out) >= 2 and out[-2] == xi and out[-1] == yi:
            continue
        out += (xi, yi)
    return out


# --- coordinates ---------------------------------------------------------------------


# --- compact block layers -------------------------------------------------------------

class Layer:
    """One layer of a block as flat int32 arrays instead of nested lists.

    Every row has a property tuple (``props[pi[i]]``; repeated tuples are shared).
    Points: ``co[2i], co[2i+1]``. Lines: vertices ``vo[i]..vo[i+1]``. Areas: rings
    ``rs[i]..rs[i+1]``, ring r being vertices ``vo[r]..vo[r+1]``. Vertex v is
    ``co[2v], co[2v+1]``; lines and areas also have a bounding box, ``bb[4i:4i+4]``.
    """

    __slots__ = ("kind", "props", "pi", "bb", "rs", "vo", "co")
    ARRAYS = {"point": ("pi", "co"), "line": ("pi", "bb", "vo", "co"), "area": ("pi", "bb", "rs", "vo", "co")}

    def __init__(self, kind: str):
        self.kind = kind
        self.props: list = []
        self.pi = array("i")
        self.bb = array("i")
        self.rs = array("i", [0])
        self.vo = array("i", [0])
        self.co = array("i")

    @property
    def n(self) -> int:
        return len(self.pi)

    def nbytes(self) -> int:
        return sum(len(getattr(self, a)) * 4 for a in self.ARRAYS[self.kind]) + 120 * len(self.props) + 400

    def bbox(self, i: int) -> tuple[int, int, int, int]:
        b = 4 * i
        return self.bb[b], self.bb[b + 1], self.bb[b + 2], self.bb[b + 3]

    def rings(self, i: int) -> range:
        return range(self.rs[i], self.rs[i + 1])

    def pts(self, v0: int, v1: int) -> list[tuple[int, int]]:
        co = self.co
        return [(co[2 * v], co[2 * v + 1]) for v in range(v0, v1)]


class LayerBuilder:
    """Appends rows to a Layer, sharing repeated property tuples."""

    def __init__(self, kind: str):
        self.layer = Layer(kind)
        self._index: dict[tuple, int] = {}
        self.seen: set | None = None  # set when one feature may come twice (under two type names)

    def _row(self, props) -> None:
        key = tuple(props)
        i = self._index.get(key)
        if i is None:
            i = self._index[key] = len(self.layer.props)
            self.layer.props.append(key)
        self.layer.pi.append(i)

    def point(self, props, x: float, y: float) -> None:
        self._row(props)
        self.layer.co.extend((round(x), round(y)))

    def _vertices(self, pts) -> tuple[int, int, int, int] | None:
        """Adds a run of vertices (dropping repeats); its bbox, or None if under two."""
        co, vo = self.layer.co, self.layer.vo
        start = len(co)
        lx = ly = None
        for x, y in pts:
            x, y = round(x), round(y)
            if x == lx and y == ly:
                continue
            co.append(x)
            co.append(y)
            lx, ly = x, y
        if len(co) - start < 4:
            del co[start:]
            return None
        xs, ys = co[start::2], co[start + 1::2]
        vo.append(len(co) // 2)
        return min(xs), min(ys), max(xs), max(ys)

    def line(self, props, pts) -> None:
        bb = self._vertices(pts)
        if bb is not None:
            self._row(props)
            self.layer.bb.extend(bb)

    def area(self, props, rings) -> None:
        L = self.layer
        boxes = []
        for ring in rings:
            mark = len(L.vo)
            bb = self._vertices(ring)
            if bb is not None and L.vo[-1] - L.vo[-2] >= 3:
                boxes.append(bb)
            elif bb is not None:  # fewer than three corners: no area
                del L.co[2 * L.vo[-2]:]
                del L.vo[mark:]
        if not boxes:
            return
        self._row(props)
        L.rs.append(len(L.vo) - 1)
        L.bb.extend((min(b[0] for b in boxes), min(b[1] for b in boxes),
                     max(b[2] for b in boxes), max(b[3] for b in boxes)))


def _scalar(value: Any) -> Any:
    """A property value that can be stored and hashed: lists become comma lists."""
    if isinstance(value, (list, tuple)):
        return ",".join(str(v) for v in value)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    return value


def layers_from_rows(rows: dict[str, list]) -> dict[str, Layer]:
    """Layers from rows in the old list form: areas [props, bbox, rings], lines
    [props, bbox, line], points [props, x, y]. Used by tests and to convert old files."""
    out = {}
    for key, items in rows.items():
        kind = SOURCES[key][2] if key in SOURCES else "point"
        b = LayerBuilder(kind)
        for row in items:
            _add_row(b, row)
        out[key] = b.layer
    return out


def _add_row(b: LayerBuilder, row: list) -> None:
    if b.layer.kind == "point":
        b.point([_scalar(v) for v in row[0]], row[1], row[2])
    elif b.layer.kind == "line":
        b.line([_scalar(v) for v in row[0]], row[2])
    else:
        b.area([_scalar(v) for v in row[0]], row[2])


def _dp(co: array, v0: int, v1: int, tol: float) -> list[int]:
    """Douglas-Peucker on vertices v0..v1-1 of a flat coordinate array: the ones to keep."""
    n = v1 - v0
    if n < 3 or tol <= 0:
        return list(range(v0, v1))
    keep = bytearray(n)
    keep[0] = keep[-1] = 1
    tol2 = tol * tol
    stack = [(0, n - 1)]
    while stack:
        a, b = stack.pop()
        ax, ay = co[2 * (v0 + a)], co[2 * (v0 + a) + 1]
        bx, by = co[2 * (v0 + b)], co[2 * (v0 + b) + 1]
        dx, dy = bx - ax, by - ay
        len2 = dx * dx + dy * dy
        best, idx = -1.0, -1
        for i in range(a + 1, b):
            px, py = co[2 * (v0 + i)], co[2 * (v0 + i) + 1]
            if len2 == 0:
                d = (px - ax) ** 2 + (py - ay) ** 2
            else:
                t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / len2))
                d = (px - ax - t * dx) ** 2 + (py - ay - t * dy) ** 2
            if d > best:
                best, idx = d, i
        if best > tol2:
            keep[idx] = 1
            stack.append((a, idx))
            stack.append((idx, b))
    return [v0 + i for i in range(n) if keep[i]]


def _point_shown(key: str, props: tuple, max_z: int) -> bool:
    """Whether a point could be drawn at or below a zoom (zoomed-out levels skip the rest)."""
    if key == "aid":
        return max_z >= 12 or AID_TYPES.get(str(props[0] or "").lower()) in MAJOR_AIDS
    if key == "name":
        text = str(props[0] or "")
        code = _int(props[2]) if len(props) > 2 else None
        return _name_min_z(code, _name_kind(code, text), text) <= max_z
    return True


def _multiple(value: Any, step: float) -> bool:
    v = _num(value)
    return v is not None and abs(v / step - round(v / step)) < 1e-6


def _shown(key: str, props: tuple, max_z: int, classes: dict[int, int]) -> bool:
    """Whether a line or area could be drawn at or below a zoom."""
    if key == "contour":
        d = _num(props[0])
        return d in CONTOURS and not (max_z < 12 and d < 10) and not (max_z <= 9 and d not in OVERVIEW_CONTOURS)
    if key == "height":
        return max_z > 13 or _multiple(props[0], 10)  # zoom 12 shows every 20 m, 13 every 10 m
    if max_z <= OVERVIEW_LEVEL[0]:
        if key == "tss":
            return props[0] in OVERVIEW_TSS or props[0] in WATER_TSS
        if key == "tssline":
            return props[0] in OVERVIEW_TSSLINE
        if key == "fline":
            return classes.get(_int(_pad(props, 2)[1])) in OVERVIEW_FAIRWAYS
    return True


def _reduce(key: str, layer: Layer, max_z: int, tol: float, smallest: int,
            classes: dict[int, int] | None = None) -> Layer:
    """A simplified copy of a layer for zoomed-out tiles."""
    b = LayerBuilder(layer.kind)
    co = layer.co
    classes = classes or {}
    for i in range(layer.n):
        props = layer.props[layer.pi[i]]
        if layer.kind == "point":
            if _point_shown(key, props, max_z):
                b.point(props, co[2 * i], co[2 * i + 1])
            continue
        x0, y0, x1, y1 = layer.bbox(i)
        if x1 - x0 < smallest and y1 - y0 < smallest:
            continue
        if not _shown(key, props, max_z, classes):
            continue
        if key == "fline" and max_z <= OVERVIEW_LEVEL[0]:
            # Overviews have no routes to look the fairway class up in: it goes with the line.
            props = (*_pad(props, 2)[:2], classes.get(_int(_pad(props, 2)[1])))
        if layer.kind == "line":
            b.line(props, [(co[2 * v], co[2 * v + 1]) for v in _dp(co, layer.vo[i], layer.vo[i + 1], tol)])
        else:
            rings = []
            for r in layer.rings(i):
                v0, v1 = layer.vo[r], layer.vo[r + 1]
                xs, ys = co[2 * v0:2 * v1:2], co[2 * v0 + 1:2 * v1:2]
                if max(xs) - min(xs) < smallest and max(ys) - min(ys) < smallest:
                    continue  # an islet or a pool too small to see, rather than a speck
                kept = _dp(co, v0, v1, tol)
                if len(kept) >= 3:
                    rings.append([(co[2 * v], co[2 * v + 1]) for v in kept])
            b.area(props, rings)
    return b.layer


def make_levels(full: dict[str, Layer]) -> list[dict[str, Layer]]:
    """The levels of detail stored in a block file, from the full data."""
    levels = []
    for max_z, tol, smallest in LEVELS:
        if not tol:
            levels.append(full)
            continue
        levels.append({key: _reduce(key, layer, max_z, tol, smallest) for key, layer in full.items()
                       if LAYER_MIN_Z.get(key, 99) <= max_z})
    return levels


def make_overview(layers: dict[str, Layer]) -> dict[str, Layer]:
    """What of a block's layers overview charts show, much simplified: depths, the deeper
    contours, routeing, merchant fairways, major lights, towns and seas. From the full data,
    or from LEVELS[0] for files written before overviews."""
    max_z, tol, smallest = OVERVIEW_LEVEL
    route = layers.get("route")
    classes = {_int(p[1]): _int(p[0]) for p in (route.props if route else ()) if _int(p[1]) is not None}
    return {key: _reduce(key, layer, max_z, tol, smallest, classes) for key, layer in layers.items()
            if LAYER_MIN_Z.get(key, 99) <= max_z}


# --- block files ----------------------------------------------------------------------
# A block part file is gzip: a magic line, then for each level of detail a JSON header
# line (layer kinds, property tuples, array lengths) followed by the raw int32 arrays.
# Reading a zoomed-out level stops early, so it never touches the detailed data. Files
# written since overviews start with one more such section, the overview level (its
# header says "ov"), which read_block skips: level numbers stay those of LEVELS.

MAGIC = b"FWCHART2\n"
SUFFIX = ".fwc"
OLD_SUFFIX = ".json.gz"  # the earlier JSON format, converted on first use


def write_block(path: Path, meta: dict, levels: list[dict[str, Layer]],
                overview: dict[str, Layer] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    sections = ([({**meta, "ov": 1}, overview)] if overview is not None else []) + [(meta, L) for L in levels]
    with gzip.open(tmp, "wb", compresslevel=5) as f:
        f.write(MAGIC)
        for head_meta, layers in sections:
            head = {**head_meta, "layers": {key: {"k": L.kind, "p": L.props,
                                                   "n": [len(getattr(L, a)) for a in Layer.ARRAYS[L.kind]]}
                                             for key, L in layers.items()}}
            f.write(json.dumps(head, separators=(",", ":"), ensure_ascii=False).encode() + b"\n")
            for L in layers.values():
                for a in Layer.ARRAYS[L.kind]:
                    arr = getattr(L, a)
                    if sys.byteorder != "little":
                        arr = array("i", arr)
                        arr.byteswap()
                    arr.tofile(f)
    os.replace(tmp, path)


def _read_section(f, keep: bool) -> tuple[dict, dict[str, Layer]]:
    head = json.loads(f.readline())
    layers: dict[str, Layer] = {}
    for key, spec in head.pop("layers").items():
        if not keep:
            f.seek(4 * sum(spec["n"]), 1)
            continue
        L = Layer(spec["k"])
        L.props = [tuple(p) for p in spec["p"]]
        for name, n in zip(Layer.ARRAYS[spec["k"]], spec["n"]):
            arr = array("i")
            arr.fromfile(f, n)
            if sys.byteorder != "little":
                arr.byteswap()
            setattr(L, name, arr)
        layers[key] = L
    return head, layers


def read_block(path: Path, level: int) -> tuple[dict, dict[str, Layer]]:
    """(meta, layers) of one level of a block file."""
    with gzip.open(path, "rb") as f:
        if f.read(len(MAGIC)) != MAGIC:
            raise ValueError(f"{path.name} is not a chart block")
        lvl = 0
        while True:
            head, layers = _read_section(f, lvl == level)
            if head.get("ov"):
                continue  # the overview level
            if lvl == level:
                return head, layers
            lvl += 1


def read_overview(path: Path) -> dict[str, Layer]:
    """A block file's overview level; made from its first level for files older than overviews."""
    with gzip.open(path, "rb") as f:
        if f.read(len(MAGIC)) != MAGIC:
            raise ValueError(f"{path.name} is not a chart block")
        head, layers = _read_section(f, True)
    return layers if head.get("ov") else make_overview(layers)


def read_meta(path: Path) -> dict:
    """Just the header of a block file (whether layers were missing, and so on)."""
    with gzip.open(path, "rb") as f:
        if f.read(len(MAGIC)) != MAGIC:
            raise ValueError(f"{path.name} is not a chart block")
        head = json.loads(f.readline())
    head.pop("layers", None)
    return head


def _iter_old(path: Path) -> Iterator[tuple[str, Any]]:
    """Rows of an old JSON block file one at a time, so converting a big file never
    holds it all in memory: ("missing", [...]) first, then (layer, row) pairs."""
    dec = json.JSONDecoder()
    with gzip.open(path, "rt", encoding="utf-8") as f:
        buf, pos = "", 0

        def fill() -> bool:
            nonlocal buf, pos
            chunk = f.read(1 << 16)
            if not chunk:
                return False
            buf, pos = buf[pos:] + chunk, 0
            return True

        def char() -> str:
            while pos >= len(buf):
                if not fill():
                    raise ValueError("chart block ends early")
            return buf[pos]

        def value() -> Any:
            nonlocal pos
            while True:
                try:
                    v, end = dec.raw_decode(buf, pos)
                except json.JSONDecodeError:
                    if not fill():
                        raise
                    continue
                pos = end
                return v

        while '"layers":{' not in buf:
            if not fill():
                raise ValueError("no layers in chart block")
        start = buf.index('"missing":') + len('"missing":') if '"missing":' in buf else -1
        if start >= 0:
            pos = start
            yield "missing", value()
        pos = buf.index('"layers":{') + len('"layers":{')
        while char() != "}":
            if char() == ",":
                pos += 1
            key = value()
            for expected in ":[":
                if char() != expected:
                    raise ValueError("unexpected chart block layout")
                pos += 1
            while char() != "]":
                if char() == ",":
                    pos += 1
                yield key, value()
            pos += 1


def convert_old(path: Path) -> tuple[dict, list[dict[str, Layer]]]:
    """An old JSON block file as (meta, levels)."""
    builders: dict[str, LayerBuilder] = {}
    missing: list = []
    for key, row in _iter_old(path):
        if key == "missing":
            missing = row
            continue
        if key not in SOURCES:
            continue
        b = builders.get(key)
        if b is None:
            b = builders[key] = LayerBuilder(SOURCES[key][2])
        _add_row(b, row)
    full = {key: b.layer for key, b in builders.items()}
    return {"v": 2, "missing": missing}, make_levels(full)


def block_bounds(bx: int, by: int) -> tuple[float, float, float, float]:
    """Web-mercator metres of a block: (minx, miny, maxx, maxy)."""
    size = 2 * HALF / 2**BLOCK_Z
    minx = -HALF + bx * size
    maxy = HALF - by * size
    return minx, maxy - size, minx + size, maxy


def to_block(bx: int, by: int) -> Callable[[float, float], tuple[float, float]]:
    minx, _, _, maxy = block_bounds(bx, by)
    scale = UNITS / (2 * HALF / 2**BLOCK_Z)
    return lambda x, y: ((x - minx) * scale, (maxy - y) * scale)


def lonlat_to_mercator(lon: float, lat: float) -> tuple[float, float]:
    x = lon * HALF / 180
    y = math.log(math.tan((90 + lat) * math.pi / 360)) * HALF / math.pi
    return x, y


FINNISH_WATERS_M = [lonlat_to_mercator(lon, lat) for lon, lat in FINNISH_WATERS]
NEAR_M = 30000  # Finnish chart data reaches a little past FINNISH_WATERS


def _in_poly(px: float, py: float, poly: list) -> bool:
    inside = False
    k = len(poly) - 1
    for n, (xi, yi) in enumerate(poly):
        xj, yj = poly[k]
        if (yi > py) != (yj > py) and px < (xj - xi) * (py - yi) / (yj - yi) + xi:
            inside = not inside
        k = n
    return inside


def near_finland(bounds: tuple[float, float, float, float]) -> bool:
    """Whether a block could hold Finnish chart data (it touches FINNISH_WATERS, give or take)."""
    minx, miny, maxx, maxy = bounds[0] - NEAR_M, bounds[1] - NEAR_M, bounds[2] + NEAR_M, bounds[3] + NEAR_M
    poly = FINNISH_WATERS_M
    if any(minx <= x <= maxx and miny <= y <= maxy for x, y in poly):
        return True
    for i in range(5):
        for j in range(5):
            if _in_poly(minx + (maxx - minx) * i / 4, miny + (maxy - miny) * j / 4, poly):
                return True
    return False


# Kept on the NAS when "Keep chart on NAS" is on: the Gulf of Finland, Hanko to the
# eastern border (lon/lat). Blocks in it with no Finnish waters nearby are left out.
KEEP_AREA = (22.7, 59.45, 28.2, 60.75)
KEEP_PAUSE = 1.0  # s between downloads, to be gentle on Traficom and Väylävirasto
KEEP_RECHECK = 6 * 3600  # s between passes that refresh blocks older than MAX_AGE


# FINNISH_WATERS's sea border away from the mainland, from the Bothnian Bay round Åland and
# along the Gulf of Finland to off Kotka (not the top of the Bothnian Bay, where Tornio is on
# it, nor the last stretch to the land border at Virolahti), as (stretch, metres): a block
# this close to a stretch counts as on it. Closer by Kvarken's and Åland's islands, so that
# a big island is never far enough in to be outside the depth areas' holes.
SEA_BORDER = ((FINNISH_WATERS_M[3:7], 2000), (FINNISH_WATERS_M[6:10], 10000),
              (FINNISH_WATERS_M[9:13], 2000), (FINNISH_WATERS_M[12:21], 10000))


@lru_cache(maxsize=4096)
def near_sea_border(bx: int, by: int) -> bool:
    """Whether a block lies on (or near) the sea border, where there's no mainland."""
    x0, y0, x1, y1 = block_bounds(bx, by)
    return any(clip_line(line, x0 - m, y0 - m, x1 + m, y1 + m) for line, m in SEA_BORDER)


def _in_finnish_waters(bx: int, by: int, ux: float, uy: float) -> bool:
    """Whether a point in block units lies inside FINNISH_WATERS."""
    minx, _, maxx, maxy = block_bounds(bx, by)
    scale = (maxx - minx) / UNITS
    return _in_poly(minx + ux * scale, maxy - uy * scale, FINNISH_WATERS_M)


def keep_blocks(area: tuple[float, float, float, float] = KEEP_AREA) -> list[tuple[int, int]]:
    """Blocks to keep on disk for an area, nearest the middle of the gulf first."""
    n = 2**BLOCK_Z
    (x0, y1), (x1, y0) = lonlat_to_mercator(area[0], area[1]), lonlat_to_mercator(area[2], area[3])
    size = 2 * HALF / n
    bx0, bx1 = int((x0 + HALF) // size), int((x1 + HALF) // size)
    by0, by1 = int((HALF - y0) // size), int((HALF - y1) // size)
    return [(bx, by) for by in range(by0, by1 + 1) for bx in range(bx0, bx1 + 1)
            if near_finland(block_bounds(bx, by))]


def _num(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# --- turning WFS features into block data -------------------------------------------

def _rings(geom: dict) -> list[list]:
    """Polygon rings (outer and holes alike) of a Polygon or MultiPolygon."""
    if geom["type"] == "Polygon":
        return list(geom["coordinates"])
    if geom["type"] == "MultiPolygon":
        return [ring for poly in geom["coordinates"] for ring in poly]
    return []


def _lines(geom: dict) -> list[list]:
    if geom["type"] == "LineString":
        return [geom["coordinates"]]
    if geom["type"] == "MultiLineString":
        return list(geom["coordinates"])
    return []


def _points(geom: dict) -> list:
    if geom["type"] == "Point":
        return [geom["coordinates"]]
    if geom["type"] == "MultiPoint":
        return list(geom["coordinates"])
    return []



def _signature(tag: str | None, geom: dict) -> tuple:
    """Enough of a feature's geometry to tell the same feature fetched under two names."""
    coords = geom.get("coordinates")
    first = coords
    while isinstance(first, list) and first and isinstance(first[0], list):
        first = first[0]
    size = len(coords) if isinstance(coords, list) else 0
    inner = coords[0] if size and isinstance(coords[0], list) else None
    return (tag, geom.get("type"), tuple(round(v, 1) for v in (first or [])[:2]), size,
            len(inner) if isinstance(inner, list) else 0)


def _ring_area(pts: list) -> float:
    return abs(sum(ax * by - bx * ay for (ax, ay), (bx, by) in zip(pts, pts[1:] + pts[:1]))) / 2


def build_into(b: LayerBuilder, bx: int, by: int, key: str, features: Iterable[dict], tag: str | None = None,
               extra: LayerBuilder | None = None, lonlat: bool = False) -> None:
    """Clip a page of a layer's features to the block (with a margin) and add what the
    panel draws to the layer being built. ``tag`` is kept as the "_src" property, ``extra``
    gets a building's outline when it's big enough to draw, and ``lonlat`` says the
    coordinates are degrees (the Land Survey's) rather than web-mercator metres."""
    to_b = to_block(bx, by)
    if lonlat:
        def project(lon, lat):
            return to_b(*lonlat_to_mercator(lon, lat))
    else:
        project = to_b
    kind, wanted = SOURCES[key][2], SOURCES[key][3]
    margin = UNITS * TILE_BUFFER / EXTENT  # a block-sized tile's buffer, the largest needed
    lo, hi = -margin, UNITS + margin
    pm = POINT_MARGINS.get(key, BLOCK_POINT_MARGIN)
    plo, phi = -pm, UNITS + pm
    tol = FULL_TOL.get(key, 0)
    minx, miny, maxx, maxy = block_bounds(bx, by)
    # Ground metres in a block unit, for building sizes.
    unit_m = (maxx - minx) / UNITS * math.cos(math.radians(mercator_to_lonlat(0, (miny + maxy) / 2)[1]))
    for f in features:
        geom = f.get("geometry")
        if not geom or not geom.get("coordinates"):
            continue
        if b.seen is not None:
            sig = _signature(tag, geom)
            if sig in b.seen:
                continue
            b.seen.add(sig)
        raw = {k.lower(): v for k, v in (f.get("properties") or {}).items()}
        if tag is not None:
            raw["_src"] = tag
        if kind == "area":
            rings = []
            for ring in _rings(geom):
                clipped = clip_ring([project(*c[:2]) for c in ring], lo, lo, hi, hi)
                if tol:
                    clipped = simplify(clipped, tol)
                if len(clipped) >= 3:
                    rings.append(clipped)
            b.area([_scalar(raw.get(p)) for p in wanted], rings)
        elif kind == "line":
            props = [_scalar(raw.get(p)) for p in wanted]
            # Areas given for a line layer (VTS areas, pontoons) are kept as their outlines.
            for line in _lines(geom) or [ring for ring in _rings(geom)]:
                for part in clip_line([project(*c[:2]) for c in line], lo, lo, hi, hi):
                    b.line(props, simplify(part, tol) if tol else part)
        else:
            rings = _rings(geom)
            if rings:
                # An area as a point (buildings): its middle, with its size.
                outer = [project(*c[:2]) for c in rings[0]]
                x0, y0, x1, y1 = _bbox(outer)
                pts = [((x0 + x1) / 2, (y0 + y1) / 2)]
                m2 = _ring_area(outer) * unit_m * unit_m
                raw["_size"] = 3 if m2 >= BUILDING_AREA else 2 if m2 >= SMALL_BUILDING else 1
                if extra is not None and raw["_size"] == 3:
                    clipped = clip_ring(simplify(outer, FULL_TOL.get("buildarea", 0)), lo, lo, hi, hi)
                    if len(clipped) >= 3:
                        extra.area([_scalar(raw.get(p)) for p in SOURCES[DERIVED_BY[key]][3]], [clipped])
            else:
                pts = [project(*c[:2]) for c in _points(geom)] or \
                      [project(*c[:2]) for line in _lines(geom) for c in line[:1]]
            props = [_scalar(raw.get(p)) for p in wanted]
            for x, y in pts:
                if plo <= x <= phi and plo <= y <= phi:
                    b.point(props, x, y)


DERIVED_BY = {source: derived for derived, source in DERIVED.items()}


def build_block(bx: int, by: int, layers: dict[str, list[dict]]) -> dict[str, Layer]:
    """All of a block's layers from WFS features at once (tests and the simulator)."""
    out = {}
    for key, features in layers.items():
        b = LayerBuilder(SOURCES[key][2])
        extra = LayerBuilder(SOURCES[DERIVED_BY[key]][2]) if key in DERIVED_BY else None
        build_into(b, bx, by, key, features, extra=extra)
        out[key] = b.layer
        if extra is not None:
            out[DERIVED_BY[key]] = extra.layer
    return out


def join_heights(marks: Layer, labels: Layer | None) -> Layer:
    """Landmarks as (kind, height, name), the height from the landmark itself or from the
    nearest height label of its kind (the Land Survey keeps a mast's height as a text beside it)."""
    found = []
    if labels is not None:
        for i in range(labels.n):
            tag, value, text = _pad(labels.props[labels.pi[i]], 3)[:3]
            h = _num(value) if _num(value) is not None else _num(re.sub(r"[^\d.]", "", str(text or "")) or None)
            if h is not None:
                found.append((tag, labels.co[2 * i], labels.co[2 * i + 1], h))
    b = LayerBuilder("point")
    for i in range(marks.n):
        tag, h1, h2, name = _pad(marks.props[marks.pi[i]], 4)[:4]
        x, y = marks.co[2 * i], marks.co[2 * i + 1]
        height = _num(h1) if _num(h1) is not None else _num(h2)
        if height is None:
            near = [(abs(lx - x) + abs(ly - y), h) for t, lx, ly, h in found
                    if t == tag and abs(lx - x) <= HEIGHT_REACH and abs(ly - y) <= HEIGHT_REACH]
            height = min(near)[1] if near else None
        if height is not None and height == int(height):
            height = int(height)
        b.point((tag, height, str(name).strip() if name else None), x, y)
    return b.layer


def heritage_props(feature: dict) -> tuple | None:
    """A Museovirasto site as (register id, name, class, kind, dating, position accuracy,
    sub-type in English). Kind: 1 wooden wreck, 2 metal wreck, 3 other or unknown wreck,
    4 other underwater site, 5 defence site on land (bunkers, trenches, gun positions)."""
    p = {k.lower(): v for k, v in (feature.get("properties") or {}).items()}
    site = _int(p.get("mjtunnus"))
    if site is None:
        return None
    sub = str(p.get("alatyyppi") or "").lower()
    kinds = str(p.get("tyyppi") or "").lower()
    if "hylyt (puu)" in sub:
        kind = 1
    elif "hylyt (metalli)" in sub:
        kind = 2
    elif "hyly" in kinds or "hyly" in sub or "haaksirikko" in sub:
        kind = 3
    elif "puolustus" in kinds and str(p.get("vedenalainen") or "").lower() != "k":
        kind = 5
    else:
        kind = 4

    def text(key: str) -> str | None:
        v = str(p.get(key) or "").strip()
        return v or None

    return (site, text("kohdenimi"), int(p.get("_class", 2)), kind, text("ajoitus"), text("paikannustarkkuus"),
            _subtype(sub))


def _subtype(sub: str) -> str | None:
    """Museovirasto's sub-types ("tykkiasemat, taisteluhaudat") in English."""
    out: list[str] = []
    for part in sub.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        name = next((en for fi, en in MV_SUBTYPES if fi in part), part)
        if name not in out:
            out.append(name)
    return ", ".join(out) or None


def coastal_filter(sites: Layer, depth: Layer | None) -> Layer:
    """Drop land sites (kind 5) further than COAST_REACH from the sea, so only the coast and
    islands show defences. Depth area edges mark the shore; edges where a depth area was cut
    at the block margin are not shore."""
    if not any(sites.props[sites.pi[i]][3] == 5 for i in range(sites.n)):
        return sites
    cell = COAST_REACH // 2
    shore: set[tuple[int, int]] = set()
    if depth is not None:
        margin = UNITS * TILE_BUFFER / EXTENT
        lo, hi = round(-margin), round(UNITS + margin)
        co = depth.co
        for i in range(depth.n):
            for r in depth.rings(i):
                v0, v1 = depth.vo[r], depth.vo[r + 1]
                for v in range(v0, v1):
                    w = v + 1 if v + 1 < v1 else v0
                    ax, ay, bx, by = co[2 * v], co[2 * v + 1], co[2 * w], co[2 * w + 1]
                    if (ax == bx and (ax <= lo or ax >= hi)) or (ay == by and (ay <= lo or ay >= hi)):
                        continue
                    steps = max(abs(bx - ax), abs(by - ay)) // cell + 1
                    for k in range(steps + 1):
                        shore.add((int(ax + (bx - ax) * k / steps) // cell, int(ay + (by - ay) * k / steps) // cell))
    b = LayerBuilder("point")
    for i in range(sites.n):
        props = sites.props[sites.pi[i]]
        x, y = sites.co[2 * i], sites.co[2 * i + 1]
        if props[3] == 5:
            cx, cy = x // cell, y // cell
            if not any((cx + dx, cy + dy) in shore for dx in (-2, -1, 0, 1, 2) for dy in (-2, -1, 0, 1, 2)):
                continue
        b.point(props, x, y)
    return b.layer



# --- cutting tiles --------------------------------------------------------------------

UNSURVEYED = 5  # the depth band of an unsurveyed area: white, hatched by its tss row


def _depth_band(drval1: Any, drval2: Any) -> int:
    """0: drying, 1: 0-3 m, 2: 3-6 m, 3: 6-10 m, 4: deeper (white)."""
    d1 = _num(drval1)
    d2 = _num(drval2)
    if d1 is not None and d1 < 0 and (d2 is None or d2 <= 0):
        return 0
    top = d2 if d2 is not None else d1
    if top is None:
        return 4
    if top <= 3:
        return 1
    if top <= 6:
        return 2
    if top <= 10:
        return 3
    return 4


def level_for(z: int) -> int:
    """Which stored level of detail a zoom level's tiles are cut from."""
    return next(i for i, (max_z, _, _) in enumerate(LEVELS) if z <= max_z)


def _run_pts(layer: Layer, v0: int, v1: int, x0: float, y0: float, x1: float, y1: float) -> list:
    """Vertices v0..v1-1, with each run that lies wholly beyond one side of the box cut
    down to its two ends. Clipping to the box gives the same result, and a long coast
    that only touches the tile costs a few comparisons per vertex, not a tuple each."""
    co = layer.co
    out = []
    code = 0  # the outside sides every vertex of the current run shares
    last = None  # the run's latest vertex, not yet added
    for v in range(v0, v1):
        x, y = co[2 * v], co[2 * v + 1]
        c = (x < x0) | (x > x1) << 1 | (y < y0) << 2 | (y > y1) << 3
        if code and c & code:
            code &= c
            last = (x, y)
            continue
        if last is not None:
            out.append(last)
            last = None
        out.append((x, y))
        code = c
    if last is not None:
        out.append(last)
    return out


def make_tile(z: int, x: int, y: int, blocks: dict[tuple[int, int], dict | None] | Iterable[tuple]) -> dict[str, Any]:
    """The chart features for one map tile, in tile units (0..EXTENT, y down). ``blocks``
    may also be (key, data) pairs made as they're needed, so that overview tiles never
    hold more than one block's data at a time."""
    tile: dict[str, Any] = {"e": EXTENT}
    rows: dict[str, list] = {}  # layer -> [props, geometry...] or [x, y, props], before formatting
    lo, hi = -TILE_BUFFER, EXTENT + TILE_BUFFER
    tol = 0.35 if z >= 12 else 0.5
    land_rects: list[tuple[float, float, float, float]] = []
    # Blocks at the sea border: (their rect, the islands in it), painted as sea (see below).
    sea_blocks: list[tuple[tuple[float, float, float, float], list]] = []
    seen_points: set = set()
    classes: dict[int, int] = {}  # fairway number -> class (1 main merchant fairway ... 6), from the routes
    for (bx, by), data in blocks.items() if isinstance(blocks, dict) else blocks:
        if not data:
            continue
        route = data.get("route")
        for props in route.props if route else ():
            jnro, vl = _int(props[1]), _int(props[0])
            if jnro is not None and vl is not None:
                classes[jnro] = vl
        # Block units -> tile units, and the tile (with buffer) in block units for quick skips.
        if z >= BLOCK_Z:
            n = 2 ** (z - BLOCK_Z)
            size = UNITS / n
            ox, oy = (x - bx * n) * size, (y - by * n) * size
        else:
            n = 2 ** (BLOCK_Z - z)
            size = UNITS * n
            ox, oy = (x * n - bx) * UNITS, (y * n - by) * UNITS
        k = EXTENT / size
        bx0, by0 = ox + lo / k, oy + lo / k
        bx1, by1 = ox + hi / k, oy + hi / k

        def conv(pts):
            return [((px - ox) * k, (py - oy) * k) for px, py in pts]

        depth = data.get("depth")
        islands = None
        if depth is not None and depth.n:
            # Land is painted only where this block's depths are, so a block that
            # couldn't be fetched never shows up as solid land. (1 unit of overlap
            # hides the seam between blocks.)
            rect = (max(lo, -ox * k - 1), max(lo, -oy * k - 1),
                    min(hi, (UNITS - ox) * k + 1), min(hi, (UNITS - oy) * k + 1))
            if near_sea_border(bx, by):
                # Out at the sea border there's no mainland, only islands, which are holes
                # in the depth areas. The rest of Finnish waters there is sea, also where
                # Traficom's depths stop short of FINNISH_WATERS's rough border.
                islands = []
                sea_blocks.append((rect, islands))
            else:
                land_rects.append(rect)

        for key, layer in data.items():
            if key[0] == "_" or z < LAYER_MIN_Z.get(key, 99):
                continue
            out = rows.setdefault(key, [])
            props, pi = layer.props, layer.pi
            if layer.kind == "point":
                buf = POINT_BUFFERS.get(key, POINT_BUFFER)
                px0, py0 = ox - buf / k, oy - buf / k
                px1, py1 = ox + (EXTENT + buf) / k, oy + (EXTENT + buf) / k
                co = layer.co
                for i in range(layer.n):
                    px, py = co[2 * i], co[2 * i + 1]
                    if not (px0 <= px <= px1 and py0 <= py <= py1):
                        continue
                    tx, ty = round((px - ox) * k), round((py - oy) * k)
                    p = props[pi[i]]
                    seen = (key, tx, ty, p)
                    if seen in seen_points:
                        continue  # the same point from two blocks' margins
                    seen_points.add(seen)
                    out.append([tx, ty, p])
                continue
            for i in range(layer.n):
                fx0, fy0, fx1, fy1 = layer.bbox(i)
                if fx1 < bx0 or fx0 > bx1 or fy1 < by0 or fy0 > by1:
                    continue
                if (fx1 - fx0) * k < 0.6 and (fy1 - fy0) * k < 0.6:
                    continue  # too small to see at this zoom
                p = props[pi[i]]
                if layer.kind == "area":
                    rings = []
                    for j, r in enumerate(layer.rings(i)):
                        pts = _run_pts(layer, layer.vo[r], layer.vo[r + 1], bx0, by0, bx1, by1)
                        part = simplify(clip_ring(conv(pts), lo, lo, hi, hi), tol)
                        if len(part) >= 3:
                            rings.append(_flat(part))
                            if j and key == "depth" and islands is not None \
                                    and _in_finnish_waters(bx, by, layer.co[2 * layer.vo[r]],
                                                           layer.co[2 * layer.vo[r] + 1]):
                                island = clip_ring(part, *sea_blocks[-1][0])
                                if len(island) >= 3:
                                    islands.append(_flat(island))
                    if rings:
                        out.append([p, *rings])
                        if key == "rockarea":
                            # The cluster's sign goes at the middle of its whole outline, the
                            # same spot in every tile it reaches.
                            r0 = layer.rings(i)[0]
                            co = layer.co
                            v0, v1 = layer.vo[r0], layer.vo[r0 + 1]
                            cx = sum(co[2 * v] for v in range(v0, v1)) / (v1 - v0)
                            cy = sum(co[2 * v + 1] for v in range(v0, v1)) / (v1 - v0)
                            rows.setdefault("rockpt", []).append([round((cx - ox) * k), round((cy - oy) * k), p])
                    continue
                if key == "contour":
                    d = _num(p[0])
                    if d not in CONTOURS or (z < 12 and d < 10):
                        continue
                if key == "fline" and z < 11 and p[0] is None:
                    continue  # boat routes only once zoomed in
                if key == "height" and z < 14 and not _multiple(p[0], 20 if z < 13 else 10):
                    continue
                if key == "road" and (ROAD_CLASSES.get(_int(p[0]), 2) == 0
                                      or (z < 13 and ROAD_CLASSES.get(_int(p[0]), 2) > 2)):
                    continue  # paths only from zoom 13; no ferry lines or ice roads
                pts = _run_pts(layer, layer.vo[i], layer.vo[i + 1], bx0, by0, bx1, by1)
                for part in clip_line(conv(pts), lo, lo, hi, hi):
                    flat = _flat(simplify(part, tol))
                    if len(flat) >= 4:
                        out.append([p, flat])
    if land_rects or sea_blocks:
        # Land: the Finnish area, painted under the depth areas.
        minx, miny, maxx, maxy = _tile_mercator(z, x, y)
        k = EXTENT / (maxx - minx)
        ring = [((mx - minx) * k, (maxy - my) * k) for mx, my in FINNISH_WATERS_M]
        land = [_flat(part) for part in (clip_ring(ring, *rect) for rect in land_rects) if len(part) >= 3]
        # At the sea border, the Finnish area is deep water with the islands left out (one row
        # per block: the blocks' 1 unit of overlap would leave a gap in an even-odd fill).
        for rect, islands in sea_blocks:
            part = clip_ring(ring, *rect)
            if len(part) >= 3:
                rows.setdefault("sea", []).append([_flat(part), *islands])
            land += islands
        if land:
            tile["land"] = land
    if rows.get("xland"):
        # Land outside Finland, under the foreign depth areas (see foreign.py).
        tile["land"] = tile.get("land", []) + [ring for _, *rings in rows["xland"] for ring in rings]
    _format(tile, rows, z, classes)
    return {k: v for k, v in tile.items() if v != []}


def _pad(p: tuple, n: int) -> tuple:
    """Properties padded to n values (older files kept fewer for some layers)."""
    p = tuple(p)
    return p if len(p) >= n else p + (None,) * (n - len(p))


def _metres(value: Any) -> float | int | None:
    v = _num(value)
    return int(v) if v is not None and v == int(v) else v


def _building_kind(code: Any) -> str:
    code = _int(code)
    if code == CHURCH:
        return "church"
    return BUILDING_KINDS.get(code // 10 if code else 0, "other")


def _sign(props: tuple) -> tuple[str, float | int | None]:
    """A water traffic sign's (type, value): see SIGN_CODES."""
    code, kind, value, name = _pad(props, 4)[:4]
    sign = SIGN_CODES.get(_int(code))
    if sign is None:
        text = f"{kind or ''} {name or ''}".lower()
        sign = next((s for word, s in SIGN_WORDS if word in text), "other")
    v = _metres(value)
    return sign, v if v else None  # 0 is how the data says "no value"


def _format(tile: dict, rows: dict[str, list], z: int, classes: dict[int, int]) -> None:
    """Turn collected rows into what the panel reads (see static/vchart.js).

    Tile coordinates are 0..512 (``e``) with a buffer past each edge; rings and lines are
    flat [x0, y0, x1, y1, ...]. Keys and rows, by layer:

    - land: [ring, ...] the Finnish area where chart data is (painted under depths)
    - depth: [band, ...rings] band 0 drying, 1 0-3 m, 2 3-6 m, 3 6-10 m, 4 deeper, 5 unsurveyed;
      deep first. Dredged and unsurveyed areas are in here too (they also come as tss rows,
      for their limits), and at the sea border the Finnish sea beyond Traficom's depths.
    - contour: [metres, line]; farea: [type, draught, class, ...rings]; fline: [draught, class, line]
    - lead: [bearing, line]; band: [line]; restrict: [types, value, ...rings]; shore: [line]
      (shore structures, pontoons, berths, dams and gates)
    - rockarea: [kind, ...rings]; rockpt: [x, y, kind] where each rock cluster's sign goes
      (the middle of its whole outline, the same in every tile); sector: [x, y, from°, to°, colour]; light: [x, y, text]
    - aid: [x, y, type, category, lit, name, colours]; sounding: [x, y, depth]
    - rock / wreck: [x, y, kind]; heritage: see _heritage_rows; name: [x, y, text, kind]
    - tss: [kind, orient, ...rings] kind lane, zone, prec, deep, twoway, dredged or
      unsurveyed; orient a lane's direction of traffic in degrees, or null
    - tssline: [kind, line] kind sep (separation line), bnd (scheme boundary), vts (VTS area
      limit) or call (radio calling-in line)
    - cable: [kind, line] kind cable (submarine cable) or pipe (submarine pipeline)
    - pilot: [x, y] pilot boarding place; call: [x, y, name, channel, orient] calling-in point
    - sign: [x, y, type, value, name] water traffic sign, type speed (value km/h), nowake,
      noanchor, nomoor, nopass, height (value m) or other
    - height: [metres, line] land contours: every 20 m at zoom 12, 10 m at 13, all from 14
    - lake: [...rings] lakes and rivers (drawn on land, under the depth areas)
    - road: [class, line] 1 major, 2 minor, 3 track or path (from zoom 13); rail: [line]
    - building: [x, y, size] size 1 small, 2 large; buildings big enough to draw their
      outline are in buildarea: [kind, ...rings] kind home, public, industry, church or other
    - landmark: [x, y, kind, height, name] kind mast, chimney, wind, tower or church;
      height in metres or null
    - cover (overview tiles, zooms 8-9): the share of the tile's blocks the overview has, 0..1;
      where it's under 1 the rest is not drawn yet
    """
    def vl(jnro: Any) -> int | None:
        return classes.get(_int(jnro)) if jnro is not None else None

    water = [row for row in rows.get("tss", ()) if row[0][0] in WATER_TSS]
    if "depth" in rows or water or "sea" in rows:
        depth = [[_depth_band(*_pad(p, 2)[:2]), *rings] for p, *rings in rows.get("depth", ())]
        # Dredged areas in the band of their least depth (deeper when not known), unsurveyed
        # ones as UNSURVEYED; the open sea at the border as deeper water.
        depth += [[UNSURVEYED if p[0] == "unsurveyed" else _depth_band(_pad(p, 3)[2], None), *rings]
                  for p, *rings in water]
        depth += [[4, *rings] for rings in rows.get("sea", ())]
        tile["depth"] = sorted(depth, key=lambda row: -row[0])  # deep first
    if "contour" in rows:
        tile["contour"] = [[_num(p[0]), line] for p, line in rows["contour"]]
    if "farea" in rows:
        tile["farea"] = [[_int(code), _num(draught), vl(jnro), *rings]
                         for p, *rings in rows["farea"] for code, draught, jnro in [_pad(p, 3)[:3]]]
    if "fline" in rows:
        # Overview lines carry their class (there are no routes to look it up in).
        tile["fline"] = [[_num(draught), vl(jnro) if vl(jnro) is not None else _int(own), line]
                         for p, line in rows["fline"] for draught, jnro, own in [_pad(p, 3)[:3]]]
    if "lead" in rows:
        tile["lead"] = [[_num(_pad(p, 1)[0]), line] for p, line in rows["lead"]]
    if "route" in rows:
        # The small-craft trunk route (class 4) gets the chart's green band.
        tile["band"] = [[line] for p, line in rows["route"] if _int(p[0]) == 4]
    if "restrict" in rows:
        tile["restrict"] = [[p[0], _num(p[1]), *rings] for p, *rings in rows["restrict"]]
    if "shore" in rows or "pier" in rows:
        tile["shore"] = [[line] for _, line in rows.get("shore", []) + rows.get("pier", [])]
    if "tss" in rows:
        tile["tss"] = [[p[0], _num(_pad(p, 2)[1]), *rings] for p, *rings in rows["tss"]
                       if p[0] not in WATER_TSS or z >= WATER_TSS_LIMITS_Z]
    if "tssline" in rows:
        tile["tssline"] = [[p[0], line] for p, line in rows["tssline"]]
    if "cable" in rows:
        tile["cable"] = [[p[0] or "cable", line] for p, line in rows["cable"]]
    if "pilot" in rows:
        tile["pilot"] = [[px, py] for px, py, _ in rows["pilot"]]
    if "call" in rows:
        tile["call"] = [[px, py, name, str(ch) if ch is not None else None, _num(orient)]
                        for px, py, p in rows["call"] for name, ch, orient in [_pad(p, 3)[:3]]]
    if "sign" in rows:
        tile["sign"] = [[px, py, *_sign(p), _pad(p, 4)[3]] for px, py, p in rows["sign"]]
    if "height" in rows:
        tile["height"] = [[_metres(p[0]), line] for p, line in rows["height"]]
    if "lake" in rows:
        tile["lake"] = [rings for _, *rings in rows["lake"]]
    if "road" in rows:
        tile["road"] = [[ROAD_CLASSES.get(_int(p[0]), 2), line] for p, line in rows["road"]]
    if "rail" in rows:
        tile["rail"] = [[line] for _, line in rows["rail"]]
    if "buildarea" in rows:
        tile["buildarea"] = [[_building_kind(p[0]), *rings] for p, *rings in rows["buildarea"]]
    landmarks = [[px, py, *_pad(p, 3)[:3]] for px, py, p in rows.get("landmark", ())]
    if "building" in rows:
        buildings = []
        for px, py, p in rows["building"]:
            code, size = _pad(p, 2)[:2]
            if _int(code) == CHURCH:
                landmarks.append([px, py, "church", None, None])  # a church symbol, not a square
            elif size != 3:  # big ones are drawn by their outline
                buildings.append([px, py, 2 if size == 2 else 1])
        tile["building"] = buildings
    if landmarks:
        tile["landmark"] = landmarks
    if "rockarea" in rows:
        tile["rockarea"] = [[ROCK_TYPES.get(_int(_pad(p, 1)[0]), 0), *rings] for p, *rings in rows["rockarea"]]
    if "rockpt" in rows:
        tile["rockpt"] = [[px, py, ROCK_TYPES.get(_int(_pad(p, 1)[0]), 0)] for px, py, p in rows["rockpt"]]

    colours: dict[tuple[int, int], set] = {}
    for px, py, (a, b, c) in rows.get("sector", ()):
        letter = LIGHT_COLOURS.get(str(c or "").lower())
        if letter:
            colours.setdefault((px, py), set()).add(letter)
    if "sector" in rows:
        # Light sectors: [x, y, from°, to°, colour], bearings towards the light as charts give them.
        # All-round rows only give a plain light its colour, read above.
        tile["sector"] = [[px, py, a, b, str(c or "v")] for px, py, a, b, c in
                          ((px, py, _num(a), _num(b), c) for px, py, (a, b, c) in rows["sector"])
                          if a is not None and b is not None and (b - a) % 360 != 0]
    if "aid" in rows:
        aids = []
        for px, py, (kind, cat, lit, name) in rows["aid"]:
            letter = AID_TYPES.get(str(kind or "").lower(), "o")
            lit = 1 if lit == "K" else 0
            # Zoomed out, only the major lights; then lit marks; all from zoom 13, as on charts.
            if (z <= 11 and letter not in MAJOR_AIDS) or (z == 12 and not lit and letter not in MAJOR_AIDS):
                continue
            found = colours.get((px, py)) or next(
                (colours[(px + dx, py + dy)] for dx in (-1, 0, 1) for dy in (-1, 0, 1) if (px + dx, py + dy) in colours),
                set())
            aids.append([px, py, letter, _int(cat), lit, name, "".join(ch for ch in "WRGY" if ch in found)])
        tile["aid"] = aids
    if "light" in rows:
        # Light characters as printed next to the light, e.g. "Iso WRG 4s".
        # Zoomed out, only the characters of the major lights drawn (as overview charts do).
        major = None if z >= 13 else {((round(a[0]) >> 2) + dx, (round(a[1]) >> 2) + dy)
                                      for a in tile.get("aid", ()) if a[2] in MAJOR_AIDS
                                      for dx in (-1, 0, 1) for dy in (-1, 0, 1)}
        tile["light"] = [[px, py, str(t).strip()] for px, py, (t,) in rows["light"]
                         if t and str(t).strip() not in ("-", "")
                         and (major is None or (round(px) >> 2, round(py) >> 2) in major)]
    if "sounding" in rows:
        tile["sounding"] = _thin_soundings([[px, py, p[0]] for px, py, p in rows["sounding"]],
                                           64 if z == 13 else 128)  # sparse at zoom 12, as on charts
    if "rock" in rows:
        tile["rock"] = [[px, py, ROCK_TYPES.get(_int(p[0]), 1)] for px, py, p in rows["rock"]]
    if "wreck" in rows:
        tile["wreck"] = [[px, py, WRECK_TYPES.get(_int(p[0]), 1)] for px, py, p in rows["wreck"]]
    if "heritage" in rows:
        tile["heritage"] = _heritage_rows(rows["heritage"], tile.get("wreck", ()), z)
    if "name" in rows:
        tile["name"] = _names(rows["name"], tile, z)


def _heritage_rows(rows: list, wrecks, z: int) -> list:
    """Wrecks, underwater sites and coastal defences:
    [x, y, id, name, class, kind, dating, accuracy, dup, sub-type]. dup 1: a chart wreck is
    drawn at the same spot. Thinned when zoomed out, protected wrecks first."""
    rows = sorted(rows, key=lambda r: (r[2][2] != 0, r[2][3] > 3, r[2][0]))
    out, cells = [], set()
    for px, py, props in rows:
        site, name, cls, kind, dating, acc = props[:6]
        sub = props[6] if len(props) > 6 else None
        if kind == 5 and z < DEFENCE_MIN_Z:
            continue
        if z <= 11:
            cell = (px // 64, py // 64)
            if cell in cells:
                continue
            cells.add(cell)
        dup = 1 if kind <= 3 and z >= 12 and any(abs(wx - px) <= 12 and abs(wy - py) <= 12 for wx, wy, _ in wrecks) else 0
        out.append([px, py, site, name, cls, kind, dating, acc, dup, sub])
    return out


# Name endings that mark islands, islets and capes, or bays and open water, in Finnish and
# Swedish: a fallback for when the feature class doesn't say.
ISLAND_ENDINGS = ("skär", "skäret", "holm", "holmen", "holmarna", "ö", "ön", "öarna", "saari", "saaret",
                  "luoto", "luodot", "kari", "karit", "klobb", "klobben", "kobb", "kobben", "kobbarna",
                  "harun", "hällen", "grund", "grundet", "grynnan", "båda", "bådan", "udd", "udden",
                  "niemi", "näs", "näset", "kallio", "lands", "landet")
WATER_ENDINGS = ("fjärd", "fjärden", "viken", "vik", "selkä", "lahti", "sund", "sundet", "salmi",
                 "fladan", "träsk", "träsket", "järvi", "bukt", "bukten", "strömmen", "ström", "selet")
OPEN_WATER_ENDINGS = ("fjärd", "fjärden", "selkä", "selät")  # big open waters, named sooner
# Coastal towns named on overview charts, the whole coast from the Gulf of Finland to the
# Bay of Bothnia. The feature classes can't tell towns from villages.
COASTAL_TOWNS = {"Hanko", "Hangö", "Tammisaari", "Ekenäs", "Inkoo", "Ingå", "Kirkkonummi", "Kyrkslätt",
                 "Espoo", "Esbo", "Helsinki", "Helsingfors", "Porvoo", "Borgå", "Loviisa", "Lovisa",
                 "Kotka", "Hamina", "Fredrikshamn", "Paldiski", "Tallinn",
                 "Turku", "Åbo", "Naantali", "Nådendal", "Parainen", "Pargas", "Uusikaupunki", "Nystad",
                 "Rauma", "Raumo", "Pori", "Björneborg", "Kristiinankaupunki", "Kristinestad",
                 "Kaskinen", "Kaskö", "Vaasa", "Vasa", "Uusikaarlepyy", "Nykarleby", "Pietarsaari",
                 "Jakobstad", "Kokkola", "Karleby", "Raahe", "Brahestad", "Oulu", "Uleåborg", "Kemi",
                 "Tornio", "Torneå", "Maarianhamina", "Mariehamn", "Salo", "Kemiönsaari", "Kimitoön",
                 # Across the gulfs (OpenStreetMap's names, see foreign.py).
                 "Stockholm", "Norrtälje", "Östhammar", "Öregrund", "Gävle", "Söderhamn", "Hudiksvall",
                 "Sundsvall", "Härnösand", "Örnsköldsvik", "Umeå", "Skellefteå", "Piteå", "Luleå", "Haparanda",
                 "Nynäshamn", "Kapellskär", "Grisslehamn", "Sillamäe", "Kunda", "Loksa", "Haapsalu",
                 "Narva-Jõesuu", "Kärdla", "Muuga", "Sankt-Peterburg", "Санкт-Петербург", "Vyborg", "Выборг",
                 "Primorsk", "Приморск", "Ust-Luga", "Усть-Луга", "Kronshtadt", "Кронштадт"}
# Seas named on overview charts (with names like "-lahti" that small bays have too).
BIG_WATERS = {"Suomenlahti", "Finska viken", "Saaristomeri", "Skärgårdshavet", "Selkämeri", "Bottenhavet",
              "Perämeri", "Bottenviken", "Merenkurkku", "Kvarken", "Norra Kvarken", "Ahvenanmeri", "Ålands hav",
              "Itämeri", "Östersjön", "Pohjanlahti", "Bottniska viken", "Saaristomeren", "Ahvenanmeren",
              "Soome laht", "Läänemeri", "Финский залив", "Balti meri"}


def _name_kind(code: int | None, text: str = "") -> str:
    """Name class for the panel: s settlement, w water, i island or cape, l other land."""
    low = text.lower()
    if code is not None and 48000 <= code < 49000:
        return "s"
    if (code is not None and 36000 <= code < 37000) or low.endswith(WATER_ENDINGS) or text in BIG_WATERS:
        return "w"
    if code in (35060, 35070) or low.endswith(ISLAND_ENDINGS):
        return "i"
    return "l"


def _name_min_z(code: int | None, kind: str, text: str) -> int:
    """Zoom from which a name shows: big waters and towns first, islands next, inland names
    only up close."""
    if text in BIG_WATERS:
        return 8
    if kind == "s":
        return 8 if text in COASTAL_TOWNS else 13
    if text.isupper():
        return 16  # all-capital names are mostly stations, airports and the like
    if kind == "w":
        return 12 if text.lower().endswith(OPEN_WATER_ENDINGS) else 13
    if kind == "i":
        return 13
    return 15


def _water_cells(tile: dict) -> set | None:
    """32-unit cells within about 50 px of the shoreline or a depth line, or None when the
    tile has no chart data to tell by."""
    if "depth" not in tile and "land" not in tile:
        return None
    lo, hi = -TILE_BUFFER, EXTENT + TILE_BUFFER
    marked = set()
    for _, *rings in tile.get("depth", ()):
        for ring in rings:
            n = len(ring)
            for i in range(0, n, 2):
                ax, ay, bx, by = ring[i], ring[i + 1], ring[(i + 2) % n], ring[(i + 3) % n]
                if (ax == bx and ax in (lo, hi)) or (ay == by and ay in (lo, hi)):
                    continue  # the tile's own edge, where an area was cut
                steps = max(abs(bx - ax), abs(by - ay)) // 16 + 1
                for s in range(steps + 1):
                    marked.add((int(ax + (bx - ax) * s / steps) >> 5, int(ay + (by - ay) * s / steps) >> 5))
    return {(cx + dx, cy + dy) for cx, cy in marked for dx in range(-3, 4) for dy in range(-3, 4)}


def _in_water(tile: dict, px: int, py: int) -> bool:
    inside = False
    for _, *rings in tile.get("depth", ()):
        for ring in rings:
            n = len(ring)
            for i in range(0, n, 2):
                ax, ay, bx, by = ring[i], ring[i + 1], ring[(i + 2) % n], ring[(i + 3) % n]
                if (ay > py) != (by > py) and px < (bx - ax) * (py - ay) / (by - ay) + ax:
                    inside = not inside
    return inside


def _names(rows: list, tile: dict, z: int) -> list:
    names = []
    cells = _water_cells(tile) if z < 15 else None
    for px, py, (text, lang, code) in rows:
        text = str(text or "")
        if not text or ("," in text and text.isupper()):
            continue  # e.g. "HANKO, POHJOINEN": station names, not chart names
        code = _int(code)
        kind = _name_kind(code, text)
        if z < _name_min_z(code, kind, text):
            continue
        if cells is not None and kind != "i" and text not in COASTAL_TOWNS and text not in BIG_WATERS \
                and (px >> 5, py >> 5) not in cells:
            # Away from the shore: inland villages, bogs and lakes stay off the sea chart
            # until zoomed right in. Open-water names far out at sea stay.
            if kind != "w" or not _in_water(tile, px, py):
                continue
        names.append([px, py, text, kind, 0 if lang in (None, "fin", "fi") else 1])
    # Finnish first, so where both languages share a spot the panel keeps the Finnish one.
    return [row[:4] for row in sorted(names, key=lambda r: r[4])]


def _int(value: Any) -> int | None:
    v = _num(value)
    return int(v) if v is not None else None


def _thin_soundings(rows: list, cell: int = 64) -> list:
    """Keep the shallowest sounding in each small square, as charts do. The squares line
    up across tiles (cell divides EXTENT and the sounding buffer), so neighbouring tiles
    keep the same soundings near their shared edge and none is cut in half."""
    best: dict[tuple[int, int], list] = {}
    for px, py, depth in rows:
        d = _num(depth)
        if d is None:
            continue
        key = (px // cell, py // cell)
        if key not in best or d < best[key][2]:
            best[key] = [px, py, round(d, 1)]
    return list(best.values())


DEBUG_ZOOMS = (12, 11, 10, 9, 8)  # the tiles VectorChart.sample saves (closest first: its blocks then go into the overviews)
DEBUG_WAIT = 60  # s it waits for each


def _layer_stats(layer: Layer) -> dict[str, Any]:
    """Counts of a layer's rows, rings and vertices, and of its rows by properties (the first
    20), for a sample."""
    by: dict[str, int] = {}
    for i in range(layer.n):
        k = json.dumps(layer.props[layer.pi[i]], ensure_ascii=False)
        by[k] = by.get(k, 0) + 1
    return {"kind": layer.kind, "rows": layer.n, "rings": len(layer.rs) - 1 if layer.kind == "area" else None,
            "vertices": len(layer.co) // 2, "by_props": dict(sorted(by.items(), key=lambda kv: -kv[1])[:20])}


def _layer_rows(layer: Layer | None) -> list | None:
    """An area layer as [props, bbox, ring, ...] rows (flat block units), for a sample."""
    if layer is None or layer.kind != "area":
        return None
    co = layer.co
    return [[list(layer.props[layer.pi[i]]), list(layer.bbox(i)),
             *[list(co[2 * layer.vo[r]:2 * layer.vo[r + 1]]) for r in layer.rings(i)]] for i in range(layer.n)]


def _tile_mercator(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    size = 2 * HALF / 2**z
    minx = -HALF + x * size
    maxy = HALF - y * size
    return minx, maxy - size, minx + size, maxy


def blocks_for(z: int, x: int, y: int) -> list[tuple[int, int]]:
    if z >= BLOCK_Z:
        s = z - BLOCK_Z
        return [(x >> s, y >> s)]
    n = 2 ** (BLOCK_Z - z)
    return [(x * n + i, y * n + j) for j in range(n) for i in range(n)]

class _Skipped(Exception):
    pass


def mercator_to_lonlat(x: float, y: float) -> tuple[float, float]:
    return x * 180 / HALF, math.degrees(2 * math.atan(math.exp(y * math.pi / HALF)) - math.pi / 2)


def _mml_features(page: dict) -> Iterator[dict]:
    """MML features (lon/lat, for build_into's ``lonlat``) with the name text under one key.
    Each is taken out of the page as it's used, so a big page is never held twice."""
    feats = page.get("features") or []
    feats.reverse()
    while feats:
        f = feats.pop()
        geom = f.get("geometry")
        if not geom or not geom.get("coordinates"):
            continue
        props = f.get("properties") or {}
        text = props.get("teksti") or props.get("nimi") or props.get("teksti_fin") or props.get("name")
        if isinstance(text, dict):  # a text with its language
            text = text.get("teksti") or text.get("text") or next(iter(text.values()), None)
        props["teksti"] = text
        yield f


def wfs_type_names(caps: str) -> set[str]:
    """Feature type names in a WFS GetCapabilities document."""
    names = set()
    for block in re.findall(r"<(?:\w+:)?FeatureType\b.*?</(?:\w+:)?FeatureType>", caps, re.S):
        m = re.search(r"<(?:\w+:)?Name>\s*([^<\s]+)\s*</(?:\w+:)?Name>", block)
        if m:
            names.add(m.group(1))
    return names


def _wfs_match(name: str, names: set[str]) -> str | None:
    """A type name as a WFS lists it: the same, or with another prefix or none (matched by
    the part after the colon, ignoring case)."""
    if name in names:
        return name
    local = name.split(":")[-1].lower()
    return next((n for n in sorted(names) if n.split(":")[-1].lower() == local), None)


def _mml_match(candidate: str, ids: set[str]) -> str | None:
    """A Land Survey collection id for a guessed one: the same, or the same but for case,
    underscores and dashes."""
    if candidate in ids:
        return candidate
    norm = candidate.lower().replace("_", "").replace("-", "")
    return next((i for i in sorted(ids) if i.lower().replace("_", "").replace("-", "") == norm), None)


def _unknown_type(err: Exception) -> bool:
    """Whether a failed request says the service has no such layer (rather than being down)."""
    if not isinstance(err, _HTTPError):
        return False
    text = err.text.lower()
    # 200: some WFS servers send their exception report with it.
    return err.status == 404 or (err.status in (200, 400) and any(
        s in text for s in ("typename", "feature type", "unknown", "not found", "no such", "does not exist")))


def _with_limit(url: str, params: dict[str, str] | None, limit: int) -> tuple[str, dict[str, str] | None]:
    """The same request for fewer features a page (``limit`` in the params, or in a next-page link)."""
    if params is not None:
        return url, {**params, "limit": str(limit)}
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != "limit"]
    return urlunsplit(parts._replace(query=urlencode(query + [("limit", str(limit))]))), None



def finish_part(built: dict[str, Layer]) -> dict[str, Layer]:
    """A fetched part as it's stored: landmarks get their heights and the height labels go."""
    labels = built.pop("markh", None)
    if "landmark" in built:
        built["landmark"] = join_heights(built["landmark"], labels)
    return built


# --- overviews (zooms 8-9) ------------------------------------------------------------
# Overview tiles are cut from the overview level at the head of each block file on disk,
# not from downloads of their own: a WFS query over a large area would bring every vertex
# of the archipelago's depth areas only to throw nearly all of it away, where the blocks
# already hold a simplified copy. A tile reads its blocks (16 at zoom 9, 64 at zoom 8) one
# at a time and keeps nothing in RAM; a whole region's overview kept in memory would take
# 100 MB and more in the archipelago. Cut tiles are kept on disk until a block in them
# changes. So the overview shows the blocks that have been fetched (all of the Gulf of
# Finland with "Keep chart on NAS"), and tiles say how much of them is there ("cover").

# Parts with anything for overviews; the rest aren't read.
OVERVIEW_PARTS = tuple(p for p, keys in PARTS.items()
                       if any(LAYER_MIN_Z.get(k, 99) <= OVERVIEW_LEVEL[0] for k in keys))


def overview_blocks(files: dict[tuple[int, int], list[Path]]) -> Iterator[tuple[tuple[int, int], dict]]:
    """Each block's overview layers, read as they're asked for (for make_tile). ``files``
    are a block's part files in PARTS order: a layer in a later part replaces an earlier one's."""
    from .foreign import merge_into
    for key, paths in files.items():
        layers: dict[str, Layer] = {}
        for path in paths:
            try:
                if path.name.endswith(f".{FOREIGN_PART}{SUFFIX}"):
                    merge_into(layers, read_overview(path))  # foreign rows add to the Finnish ones
                    continue
                layers.update(read_overview(path))
            except (OSError, ValueError, EOFError, KeyError) as err:
                _LOGGER.debug("Overview skips %s: %s", path.name, err)
        yield key, layers


class VectorChart:
    def __init__(self, directory: Path, session: aiohttp.ClientSession | None,
                 fetch_layer: Callable[[str, tuple[float, float, float, float]], Any] | None = None,
                 mml_key: str = ""):
        self.dir = directory
        self.session = session
        # None: the real services, a page at a time. A stand-in may have a ``catalog``
        # attribute, service url -> the type names it has, standing in for capabilities.
        self.fetch_layer = fetch_layer
        self.mml_key = mml_key
        self._memory: OrderedDict[tuple[int, int, int], tuple[dict, int]] = OrderedDict()
        self._memory_bytes = 0
        self._pending: dict[tuple[int, int, int], asyncio.Task] = {}
        self._fetching: dict[tuple, asyncio.Task] = {}  # part downloads and conversions in flight
        self._wanted: dict[tuple[int, int], float] = {}  # when a block was last asked for
        self._limit = asyncio.Semaphore(1)  # one download at a time: gentle on the services and on RAM
        self._reading = asyncio.Semaphore(2)  # block files read at once
        self._cutting = asyncio.Semaphore(1)
        self._failed: dict[tuple, float] = {}
        self._down: dict[str, float] = {}  # service -> when it last timed out or dropped
        # file name -> (mtime, some layer failed, type names the services didn't have then)
        self._missing: dict[str, tuple[float, bool, tuple]] = {}
        self._regions: OrderedDict[tuple[int, int], Layer] = OrderedDict()  # heritage, mercator metres
        self._coast: OrderedDict[tuple[int, int], tuple] = OrderedDict()  # block -> (region, coastal sites)
        self._caps: dict[str, set[str]] = {}  # service -> type names (collection ids) it has
        self._caps_at: dict[str, float] = {}  # service -> when they were read
        self._caps_loaded = False
        self._absent: set[str] = set()  # type names a service answered it doesn't have
        self._logged: dict[str, str] = {}  # what was last logged about a thing, so it's logged once
        self._logged_props: set[str] = set()
        self.errors: dict[str, str] = {}  # layer -> last error, for the panel
        self.blocks_fetched = 0
        self.keep_status: dict[str, Any] | None = None  # progress of keep(), None while it's off
        self._last_tile = 0.0
        self._idle: tuple[asyncio.AbstractEventLoop, asyncio.TimerHandle] | None = None
        from .foreign import ForeignSource
        self.foreign = ForeignSource(self)  # outside Finnish waters

    def set_foreign(self, on: bool) -> None:
        """Draw the Swedish and Estonian side (it costs some RAM), or Finnish waters only."""
        on = bool(on) and self.foreign.available
        if on != self.foreign.enabled:
            self.foreign.enabled = on
            self.foreign.forget()
            self._forget()

    def set_mml_key(self, key: str) -> None:
        """A new Land Survey key: blocks are read again so rocks, names and land get fetched."""
        self.mml_key = key.strip()
        self._forget()
        self._failed = {k: t for k, t in self._failed.items() if k[-1] not in MML_PARTS and k[-1] != MML_API}
        self._caps.pop(MML_API, None)
        self._caps_at.pop(MML_API, None)
        for part in MML_PARTS:
            for layer in PARTS[part]:
                self.errors.pop(layer, None)

    def _forget(self) -> None:
        self._memory.clear()
        self._memory_bytes = 0

    def _touch(self) -> None:
        """A tile was asked for: RAM caches are kept until IDLE_FORGET after the last one."""
        self._last_tile = time.monotonic()
        loop = asyncio.get_running_loop()
        if self._idle is None or self._idle[0] is not loop:
            self._idle = (loop, loop.call_later(IDLE_FORGET, self._idle_check))

    def _idle_check(self) -> None:
        loop = self._idle[0]
        left = IDLE_FORGET - (time.monotonic() - self._last_tile)
        if left > 0:
            self._idle = (loop, loop.call_later(left, self._idle_check))
            return
        self._idle = None
        freed = self._memory_bytes / 1e6
        self._forget()
        self._coast.clear()
        self._regions.clear()
        self.foreign.forget()
        _LOGGER.debug("Chart not looked at for %s s: %.1f MB let go", IDLE_FORGET, freed)

    def _scrub(self, text: str) -> str:
        """Text for a log or the panel, without the API key in it."""
        return text.replace(self.mml_key, "***") if self.mml_key else text

    def _log_once(self, what: str, msg: str, *args: Any) -> None:
        line = msg % args
        if self._logged.get(what) != line:
            self._logged[what] = line
            _LOGGER.info("%s", self._scrub(line))

    async def check_mml_key(self, key: str) -> str | None:
        """None if the Land Survey accepts the key, else what went wrong."""
        if self.session is None:
            return None
        try:
            await self._get_json(f"{MML_API}/collections/vesikivi/items",
                                 {"bbox": "24.40,59.98,24.42,59.99", "limit": "1", "api-key": key.strip()}, "rock")
        except aiohttp.ClientError as err:
            return str(err).replace(key.strip(), "***") if key.strip() else str(err)
        except (asyncio.TimeoutError, ValueError) as err:
            return str(err) or type(err).__name__
        return None

    def _parts(self, level: int | None = None) -> list[str]:
        """The parts blocks are made of (those a tile of a level of detail needs)."""
        max_z = LEVELS[level][0] if level is not None else 99
        return [part for part in PARTS if (part not in MML_PARTS or self.mml_key)
                and (part != FOREIGN_PART or self.foreign.enabled) and PART_MIN_Z.get(part, 0) <= max_z]

    def _path(self, bx: int, by: int, part: str = "", suffix: str = SUFFIX) -> Path:
        return self.dir / "blocks" / f"{bx}_{by}{'.' + part if part else ''}{suffix}"

    def status(self) -> dict[str, Any]:
        root = self.dir / "blocks"
        files = list(root.iterdir()) if root.exists() else []
        blocks = {f.name.split(".")[0] for f in files if f.name.endswith((SUFFIX, OLD_SUFFIX))}
        return {"blocks": len(blocks),
                "mb": round(sum(f.stat().st_size for f in files if f.is_file()) / 1e6, 1),
                "errors": {**self.errors, **self.foreign.errors}, "attribution": ATTRIBUTION, "min_zoom": MIN_Z, "block_zoom": BLOCK_MIN_Z,
                "keep": self.keep_status, "mml_key": bool(self.mml_key), "foreign": self.foreign.enabled,
                "memory_mb": round(self._memory_bytes / 1e6, 1),
                "layers": self.layer_report(), "rev": self._rev()}

    def _rev(self) -> str:
        """Changes whenever tiles would: a new tile format, or parts added or dropped."""
        return f"{TILE_REV}{''.join(p[:1] for p in self._parts())}"

    # --- which layers the services have ---

    def layer_report(self) -> dict[str, list[str]]:
        """Type names found and missing at the services whose capabilities have been read."""
        found, missing = set(), set()
        for key in SOURCES:
            for _, name in source_names(key):
                service = service_of(name)
                if service == MML_API and not self.mml_key:
                    continue
                got = self._resolve(name)
                if got is None:
                    missing.add(name)
                elif service in self._caps:
                    found.add(name if got == name else f"{name} ({got})")
        return {"found": sorted(found), "missing": sorted(missing)}

    def _resolve(self, name: str) -> str | None:
        """The name to ask a service for, or None when it's known not to have it. Names are
        tried as they are while a service's capabilities can't be read."""
        if name in self._absent:
            return None
        service = service_of(name)
        have = self._caps.get(service)
        if have is None:
            return name
        return _mml_match(name, have) if service == MML_API else _wfs_match(name, have)

    def _targets(self, key: str) -> tuple[list[tuple[str | None, str, str]], list[str]]:
        """A layer's (tag, type name, name to ask for) the services have, and the type names they don't."""
        targets, absent = [], []
        for tag, name in source_names(key):
            # Depths are always asked for: without them there's no chart, and a block made
            # without them because of a misread list would stay blank for a month.
            got = self._resolve(name) or (name if key == "depth" and name not in self._absent else None)
            if got is None:
                absent.append(name)
            else:
                targets.append((tag, name, got))
        return targets, absent

    def _caps_path(self) -> Path:
        return self.dir / "layers.json"

    def _load_caps(self) -> None:
        if self._caps_loaded:
            return
        self._caps_loaded = True
        try:
            saved = json.loads(self._caps_path().read_text())["services"]
        except (OSError, ValueError, KeyError, TypeError):
            return
        for service, entry in saved.items():
            if service not in self._caps and isinstance(entry, dict):
                self._set_caps(service, set(entry.get("names") or ()), entry.get("t", 0), save=False)

    def _set_caps(self, service: str, names: set[str], at: float | None = None, save: bool = True) -> None:
        wanted = sorted({name for key in SOURCES for _, name in source_names(key) if service_of(name) == service})
        match = _mml_match if service == MML_API else _wfs_match
        if not any(match(n, names) for n in wanted):
            # A list that has none of what the chart has always fetched is more likely misread
            # than right: rather try every layer than draw none.
            self._log_once(f"caps {service}", "Chart layers at %s: none of ours among %s listed; trying them all",
                           urlsplit(service).netloc, len(names))
            self._failed[("caps", service)] = time.time()
            return
        self._caps[service] = names
        self._caps_at[service] = time.time() if at is None else at
        self._absent = {n for n in self._absent if service_of(n) != service}
        if save:
            try:
                self.dir.mkdir(parents=True, exist_ok=True)
                tmp = self._caps_path().with_suffix(".tmp")
                tmp.write_text(json.dumps({"services": {s: {"t": self._caps_at[s], "names": sorted(n)}
                                                        for s, n in self._caps.items()}}))
                os.replace(tmp, self._caps_path())
            except OSError as err:
                _LOGGER.warning("Could not store the chart layer list: %s", err)
        found =[n for n in wanted if self._resolve(n)]
        missing = [n for n in wanted if not self._resolve(n)]
        self._log_once(f"caps {service}", "Chart layers at %s: found %s; missing %s", urlsplit(service).netloc,
                       ", ".join(found) or "none", ", ".join(missing) or "none")
        if service == MML_API and missing:
            # The guessed collection ids that didn't match, beside the real ones, to put right.
            self._log_once("mml ids", "Land Survey collections: %s", ", ".join(sorted(names)))

    async def _discover(self, service: str) -> None:
        """Read which layers a service has (WFS capabilities, or the Land Survey's collection
        list) once a day, kept on disk so a restart needn't ask again."""
        if self.fetch_layer is not None:
            catalog = getattr(self.fetch_layer, "catalog", None)
            if catalog is not None and service not in self._caps:
                names = catalog(service)
                if names is not None:
                    self._set_caps(service, set(names), save=False)
            return
        self._load_caps()
        if self.session is None or time.time() - self._caps_at.get(service, 0) < CAPS_MAX_AGE:
            return
        if (service == MML_API and not self.mml_key) or \
                time.time() - self._failed.get(("caps", service), 0) < FAILED_RETRY:
            return
        key = ("caps", service)
        task = self._fetching.get(key)
        if task is None:
            task = asyncio.create_task(self._read_caps(service))
            self._fetching[key] = task
            task.add_done_callback(lambda _t, key=key: self._fetching.pop(key, None))
        await asyncio.shield(task)

    async def _read_caps(self, service: str) -> None:
        try:
            if service == MML_API:
                page = await self._get_json(f"{MML_API}/collections", {"f": "json", "api-key": self.mml_key},
                                            "collections")
                names = {c["id"] for c in page.get("collections") or [] if isinstance(c, dict) and c.get("id")}
            else:
                body = await self._get_bytes(service, {"service": "WFS", "version": "2.0.0",
                                                       "request": "GetCapabilities"}, "capabilities")
                names = await asyncio.to_thread(wfs_type_names, body.decode("utf-8", "replace"))
            if not names:
                raise ValueError("no layers listed")
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as err:
            self._failed[("caps", service)] = time.time()
            self._log_once(f"caps {service}", "Could not read which chart layers %s has (%s); trying them all",
                           urlsplit(service).netloc, str(err) or type(err).__name__)
            return
        self._set_caps(service, names)

    def _fresh(self, key: tuple[int, int]) -> bool:
        for part in self._parts():
            age = _age(self._path(*key, part))
            if age is None or age >= MAX_AGE or self._path(*key, part, OLD_SUFFIX).exists():
                return False
        return True

    def _settled(self, key: tuple[int, int]) -> bool:
        """Fresh, or whatever isn't failed recently and will be tried again later: what the
        download progress counts, so one service being down doesn't hold it at nought."""
        now = time.time()
        for part in self._parts():
            age = _age(self._path(*key, part))
            if (age is None or age >= MAX_AGE) and now - self._failed.get((*key, part), 0) >= FAILED_RETRY:
                return False
        return True

    async def keep(self, blocks: list[tuple[int, int]] | None = None) -> None:
        """Download every block of KEEP_AREA to disk, one at a time, and keep them fresh.

        Runs until cancelled. Panel requests still go first: they queue for the same
        one-download-at-a-time lock, and this only asks for its next block after the
        previous one is done. Nothing is read into memory here.
        """
        blocks = keep_blocks() if blocks is None else blocks
        try:
            while True:
                done = sum(self._settled(key) for key in blocks)
                self.keep_status = {"total": len(blocks), "done": done, "running": done < len(blocks),
                                    "failed": 0, "last_ok": self.keep_status and self.keep_status.get("last_ok")}
                failures = 0
                for key in blocks:
                    await self._ensure_heritage(*key)
                    if self._fresh(key):
                        continue
                    self._wanted[key] = math.inf  # never skipped as "the map moved on"
                    ok = True
                    for part in self._parts():
                        ok = await self._ensure_part(*key, part) and ok
                    ok = ok and self._fresh(key)
                    self.keep_status["done"] = sum(self._settled(k) for k in blocks)
                    self.keep_status["failed"] = sum(not self._fresh(k) and self._settled(k) for k in blocks)
                    if ok:
                        self.keep_status["last_ok"] = time.time()
                    failures = 0 if ok else failures + 1
                    # Several failures in a row back off, so a service that's down isn't hammered;
                    # one failed area just moves on to the next.
                    await asyncio.sleep(KEEP_PAUSE if failures < 3 else min(600, 60 * (failures - 2)))
                self.keep_status["running"] = False
                await asyncio.sleep(KEEP_RECHECK)
        finally:
            self.keep_status = None

    async def tile(self, z: int, x: int, y: int,
                   wait: float | None = None) -> tuple[dict[str, Any], bool] | None:
        """The tile, and whether every block it needs was available.

        With ``wait``, gives up after that many seconds and returns None while the
        blocks keep downloading in the background: a browser only opens a few
        connections to Home Assistant, and a tile request held open for half a
        minute would stall the rest of the panel (and Home Assistant's own UI).
        """
        self._touch()
        if z < BLOCK_MIN_Z:
            return await self._overview_tile(z, x, y, wait)
        keys = blocks_for(z, x, y)
        level = level_for(z)
        now = time.monotonic()
        if len(self._wanted) > 1024:
            self._wanted = {k: t for k, t in self._wanted.items() if now - t < FORGET_AFTER}
        for key in keys:
            self._wanted[key] = now
        tasks = [self._block_task(key, level) for key in keys]
        regions = {self._region(*key) for key in keys} if z >= LAYER_MIN_Z["heritage"] else set()
        heritage = [self._ensure_heritage_task(*r) for r in regions]
        pending = [t for t in tasks + heritage if not t.done()]
        if pending:
            done, still = await asyncio.wait([asyncio.shield(t) for t in pending], timeout=wait)
            if still:
                return None
        loaded = [t.result() for t in tasks]
        regions_ok = all(t.result() for t in heritage)
        blocks = {}
        for key, data in zip(keys, loaded):
            if data is not None and z >= LAYER_MIN_Z["heritage"]:
                data = {**data, "heritage": await self._coastal_sites(key, data)}
            blocks[key] = data
        async with self._cutting:  # one tile cut at a time keeps the web server responsive
            tile = await asyncio.to_thread(make_tile, z, x, y, blocks)
        return tile, regions_ok and all(b is not None and b["_complete"] for b in loaded)

    async def sample(self, lon: float, lat: float, zooms: Iterable[int] = DEBUG_ZOOMS) -> dict[str, Any]:
        """What the chart has at a spot, for working out what goes wrong there (the panel's
        "Save chart tile on long press"): the tiles over it at several zooms as the panel
        gets them, and the depth areas of the block under it at each stored level."""
        mx, my = lonlat_to_mercator(lon, lat)
        out: dict[str, Any] = {"lon": lon, "lat": lat, "rev": self._rev(), "made": time.time(), "tiles": {}}
        for z in zooms:
            n = 2 ** z
            x = min(n - 1, max(0, int((mx + HALF) / (2 * HALF) * n)))
            y = min(n - 1, max(0, int((HALF - my) / (2 * HALF) * n)))
            result = await self.tile(z, x, y, wait=DEBUG_WAIT)
            out["tiles"][f"{z}/{x}/{y}"] = {"pending": True} if result is None else \
                {"complete": result[1], "tile": result[0]}
        n = 2 ** BLOCK_Z
        bx, by = int((mx + HALF) / (2 * HALF) * n), int((HALF - my) / (2 * HALF) * n)
        path = self._path(bx, by)

        def read() -> dict[str, Any]:
            block: dict[str, Any] = {"block": [bx, by], "near_sea_border": near_sea_border(bx, by)}
            try:
                block["meta"] = read_meta(path)
                block["age_h"] = round((time.time() - path.stat().st_mtime) / 3600, 1)
                levels = {"overview": read_overview(path)}
                for i in range(len(LEVELS)):
                    levels[str(i)] = read_block(path, i)[1]
            except (OSError, ValueError, EOFError) as err:
                block["error"] = str(err)
                return block
            block["levels"] = {name: {"layers": {k: _layer_stats(L) for k, L in layers.items()},
                                      # The detailed level is big; the stats say enough there.
                                      "depth": _layer_rows(layers.get("depth")) if name != str(len(LEVELS) - 1) else None}
                               for name, layers in levels.items()}
            return block

        async with self._reading:
            out["block"] = await asyncio.to_thread(read)
        return out

    async def block(self, bx: int, by: int, level: int = len(LEVELS) - 1) -> dict | None:
        self._wanted[(bx, by)] = time.monotonic()
        return await asyncio.shield(self._block_task((bx, by), level))

    def _block_task(self, key: tuple[int, int], level: int) -> "asyncio.Future[dict | None]":
        mkey = (*key, level)
        if mkey in self._memory:
            self._memory.move_to_end(mkey)
            future = asyncio.get_running_loop().create_future()
            future.set_result(self._memory[mkey][0])
            return future
        task = self._pending.get(mkey)
        if task is None:
            task = asyncio.create_task(self._load(*key, level))
            self._pending[mkey] = task
            task.add_done_callback(lambda _t, mkey=mkey: self._pending.pop(mkey, None))
        return task

    def _stale(self, key: tuple[int, int]) -> bool:
        return time.monotonic() - self._wanted.get(key, time.monotonic()) > FORGET_AFTER

    async def _load(self, bx: int, by: int, level: int) -> dict | None:
        parts = self._parts(level)
        have = []
        try:
            for part in parts:
                if await self._ensure_part(bx, by, part):
                    have.append(part)
                elif part == "":
                    return None  # without depths there's no chart to draw
        except _Skipped:
            return None
        async with self._reading:
            if self._stale((bx, by)):
                return None  # the map moved on while this block waited its turn
            layers = await asyncio.to_thread(self._read_parts, bx, by, have, level)
        if layers is None:
            return None
        layers["_complete"] = len(have) == len(parts)
        if layers["_complete"]:
            self._remember((bx, by, level), layers)
        return layers

    def _read_parts(self, bx: int, by: int, parts: list[str], level: int) -> dict | None:
        layers: dict[str, Any] = {}
        for part in parts:
            path = self._path(bx, by, part)
            try:
                meta, got = read_block(path, level)
            except (OSError, ValueError, EOFError) as err:
                _LOGGER.warning("Chart block file %s unreadable, fetched again later: %s", path.name, err)
                try:
                    path.unlink()
                except OSError:
                    pass
                if part == "":
                    return None
                continue
            self._missing[path.name] = (path.stat().st_mtime, _fetch_again(meta, path.name),
                                        tuple(meta.get("absent") or ()))
            if part == FOREIGN_PART:
                from .foreign import merge_into
                merge_into(layers, got)  # foreign rows add to the Finnish ones
                continue
            layers.update(got)
        return layers

    def _remember(self, key: tuple[int, int, int], layers: dict) -> None:
        size = sum(L.nbytes() for k, L in layers.items() if k[0] != "_")
        old = self._memory.pop(key, None)
        if old:
            self._memory_bytes -= old[1]
        self._memory[key] = (layers, size)
        self._memory_bytes += size
        while len(self._memory) > 1 and (self._memory_bytes > MEMORY_BYTES or len(self._memory) > MEMORY_BLOCKS):
            _, (_, dropped) = self._memory.popitem(last=False)
            self._memory_bytes -= dropped

    # --- parts on disk ---

    async def _ensure_part(self, bx: int, by: int, part: str) -> bool:
        """Make sure a block part is on disk (fetching or converting it if need be). True if
        a copy is there, even an old one after a failed download."""
        key = (bx, by, part)
        task = self._fetching.get(key)
        if task is None:
            task = asyncio.create_task(self._ensure(bx, by, part))
            self._fetching[key] = task
            task.add_done_callback(lambda _t, key=key: self._fetching.pop(key, None))
        return await asyncio.shield(task)

    async def _ensure(self, bx: int, by: int, part: str) -> bool:
        path = self._path(bx, by, part)
        old = self._path(bx, by, part, OLD_SUFFIX)
        if old.exists():
            await self._convert(old, path)
        age = _age(path)
        if age is not None and age < MAX_AGE and not (age > FAILED_RETRY and await self._has_missing(path)):
            return True
        if time.time() - self._failed.get((bx, by, part), 0) < FAILED_RETRY:
            return age is not None  # an old copy beats none
        if await self._fetch_part(bx, by, part):
            return True
        self._failed[(bx, by, part)] = time.time()
        return age is not None

    async def _has_missing(self, path: Path) -> bool:
        """Whether some layer of a part failed when it was fetched, the part's PART_REV is
        newer, or a service has since listed one it didn't have then (the part is then
        fetched again sooner)."""
        try:
            mtime = path.stat().st_mtime
        except FileNotFoundError:
            return False
        known = self._missing.get(path.name)
        if known is None or known[0] != mtime:
            try:
                meta = await asyncio.to_thread(read_meta, path)
            except (OSError, ValueError, EOFError):
                return True
            known = self._missing[path.name] = (mtime, _fetch_again(meta, path.name), tuple(meta.get("absent") or ()))
        return known[1] or any(service_of(n) in self._caps and self._resolve(n) for n in known[2])

    async def _convert(self, old: Path, path: Path) -> None:
        """Rewrite a block file from the earlier JSON format, keeping its age."""
        try:
            async with self._reading:
                mtime = old.stat().st_mtime

                def convert() -> None:
                    meta, levels = convert_old(old)
                    write_block(path, meta, levels, make_overview(levels[-1]))

                await asyncio.to_thread(convert)
                os.utime(path, (mtime, mtime))
        except FileNotFoundError:
            return
        except (OSError, ValueError, EOFError) as err:
            _LOGGER.warning("Could not convert chart block %s, fetching it again: %s", old.name, err)
        try:
            old.unlink()
        except OSError:
            pass

    async def _fetch_part(self, bx: int, by: int, part: str) -> bool:
        if part == FOREIGN_PART:
            return await self.foreign.fetch_part(bx, by)
        bounds = block_bounds(bx, by)
        size = bounds[2] - bounds[0]
        keys = PARTS[part]
        builders = {key: LayerBuilder(SOURCES[key][2]) for key in keys}
        missing: list[str] = []
        absent: list[str] = []
        near = near_finland(bounds)  # nothing to fetch out there
        fetched = [key for key in keys if key not in DERIVED] if near else []
        for service in {service_of(name) for key in fetched for _, name in source_names(key)}:
            await self._discover(service)
        started = time.monotonic()
        for i, key in enumerate(fetched):
            # The download slot is held a layer at a time, so a block someone is looking at
            # never waits for the whole of a big part being kept on disk.
            async with self._limit:
                if i == 0 and self._stale((bx, by)):
                    raise _Skipped  # the map moved on while this block waited its turn
                # Points reach further past the block than lines and areas need to.
                margin = POINT_MARGINS.get(key, BLOCK_POINT_MARGIN) if SOURCES[key][2] == "point" \
                    else UNITS * TILE_BUFFER / EXTENT
                pad = margin / UNITS * size
                bbox = (bounds[0] - pad, bounds[1] - pad, bounds[2] + pad, bounds[3] + pad)
                derived = DERIVED_BY.get(key)
                services = {service_of(name) for _, name in source_names(key)}
                down = [s for s in services if time.time() - self._down.get(s, 0) < SERVICE_DOWN_FOR]
                if down and key != "depth":
                    # A service that just timed out isn't asked again for every layer of every
                    # block: those layers count as missing and are fetched again later.
                    missing.append(key)
                    continue
                try:
                    absent += await self._fetch_into(builders[key], key, bx, by, bbox, builders.get(derived))
                    self.errors.pop(key, None)
                except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, KeyError) as err:
                    if isinstance(err, (asyncio.TimeoutError, aiohttp.ClientConnectionError)):
                        for service in services:
                            self._down[service] = time.time()
                    msg = self._scrub(str(err) or type(err).__name__)
                    if self.errors.get(key) != msg:
                        _LOGGER.warning("Chart data %s for block %s,%s failed: %s", key, bx, by, msg)
                    self.errors[key] = msg
                    if key == "depth":
                        return False  # without depths there's no chart to draw
                    builders[key] = LayerBuilder(SOURCES[key][2])
                    if derived:
                        builders[derived] = LayerBuilder(SOURCES[derived][2])
                    missing.append(key)
        if part and fetched and len(missing) == len(fetched):
            return False  # nothing of this part came through; tried again later
        built = finish_part({key: b.layer for key, b in builders.items()})
        del builders
        if "sign" in built:
            sign = built["sign"]
            self._log_once("sign codes", "Water traffic sign types seen: %s", ", ".join(sorted(
                {f"{p[0]}/{p[1]}={_sign(p)[0]}" for p in sign.props})[:40]) or "none")
        path = self._path(bx, by, part)

        def write() -> None:
            write_block(path, {"v": 2, "missing": missing, "absent": absent, "rev": PART_REV.get(part, 0)}, make_levels(built),
                        make_overview(built))

        await asyncio.to_thread(write)
        self._missing[path.name] = (path.stat().st_mtime, bool(missing), tuple(absent))
        self.blocks_fetched += 1
        _LOGGER.info("Chart block %s,%s%s: %s in %.1f s", bx, by, f" ({part})" if part else "",
                     ", ".join(f"{L.n} {k}" for k, L in built.items()), time.monotonic() - started)
        return True

    async def _fetch_into(self, builder: LayerBuilder, key: str, bx: int, by: int,
                          bbox: tuple[float, float, float, float], extra: LayerBuilder | None = None) -> list[str]:
        """Fetch one layer for a block from each of its sources, building each page into the
        layer as it comes so only one page of raw data is ever in memory. Returns the type
        names the services turned out not to have."""
        targets, absent = self._targets(key)
        if len({tag for tag, _, _ in targets}) < len(targets):
            builder.seen = set()  # two names for one thing: the same feature may come under both

        for tag, name, ask in targets:
            def add(features: Iterable[dict], tag: str | None = tag, lonlat: bool = False) -> None:
                it = iter(features)
                first = next(it, None)
                if first is None:
                    return
                if key not in self._logged_props:
                    # Logged once, to show which fields a service actually returns.
                    self._logged_props.add(key)
                    _LOGGER.info("Chart data %s fields: %s", key, ", ".join(sorted(first.get("properties") or {})))
                build_into(builder, bx, by, key, chain((first,), it), tag, extra, lonlat)

            try:
                if self.fetch_layer is not None:
                    features = await self.fetch_layer(key if tag is None else f"{key}.{tag}", bbox)
                    await asyncio.to_thread(add, features)
                elif service_of(name) == MML_API:
                    await self._fetch_mml(key, bbox, lambda feats, add=add: add(feats, lonlat=True), ask)
                else:
                    await self._fetch_wfs(key, bbox, add, ask)
            except _HTTPError as err:
                if not _unknown_type(err) or key == "depth":
                    raise
                # The service hasn't got it (its capabilities couldn't be read, or are out
                # of date): an empty layer, not a failure.
                self._absent.add(name)
                absent.append(name)
                self._log_once(f"absent {name}", "Chart layer %s is not there: %s", name, str(err)[:120])
        return absent

    async def _get_bytes(self, url: str, params: dict[str, str] | None, what: str) -> bytearray:
        for wait in GET_RETRIES:
            try:
                return await self._get_once(url, params, what)
            except (aiohttp.ClientConnectionError, aiohttp.ClientPayloadError) as err:
                # A dropped connection (often a pooled one the server had already closed, or a
                # busy server shedding load) is worth asking again after a pause.
                _LOGGER.debug("Chart %s request dropped (%s), again in %s s", what, err, wait)
                await asyncio.sleep(wait)
        return await self._get_once(url, params, what)

    async def _get_once(self, url: str, params: dict[str, str] | None, what: str) -> bytearray:
        async with self.session.get(url, params=params, headers={"User-Agent": USER_AGENT},
                                    timeout=aiohttp.ClientTimeout(total=120, sock_connect=20, sock_read=60)) as resp:
            body = bytearray()  # grown in place: never the chunks and their join at once
            async for chunk in resp.content.iter_chunked(65536):
                if len(body) + len(chunk) > MAX_RESPONSE:
                    raise _TooBig(f"more than {MAX_RESPONSE // 1048576} MB of {what} data for one area")
                body += chunk
            if resp.status != 200:
                raise _HTTPError(resp.status, self._scrub(body[:2000].decode("utf-8", "replace").strip()))
        return body

    async def _get_json(self, url: str, params: dict[str, str] | None, what: str) -> dict:
        body = await self._get_bytes(url, params, what)
        if not body[:256].lstrip().startswith(b"{"):
            # Some services answer an error with 200 and an XML exception report.
            raise _HTTPError(200, self._scrub(body[:2000].decode("utf-8", "replace").strip()))
        return await asyncio.to_thread(json.loads, body)  # off the event loop

    async def _fetch_wfs(self, key: str, bbox: tuple[float, float, float, float],
                         sink: Callable[[list[dict]], None] | None = None, name: str | None = None) -> list[dict]:
        """A WFS layer in a bbox, a page at a time. Each page goes to ``sink`` (in a
        thread) and is then dropped; without a sink the features are returned."""
        type_name = name or source_names(key)[0][1]
        url, props = service_of(type_name), SOURCES[key][3]
        params = {
            "service": "WFS", "version": "2.0.0", "request": "GetFeature", "typeNames": type_name,
            "srsName": "EPSG:3857", "outputFormat": "application/json",
            "bbox": ",".join(f"{v:.1f}" for v in bbox) + ",EPSG:3857",
        }
        if url == VAYLA_WFS:
            params["propertyName"] = ",".join((*(p for p in props if p[0] != "_"), "geom"))
        delivered = False

        def through(features: list[dict]) -> None:
            nonlocal delivered
            delivered = True
            sink(features)

        try:
            return await self._paged(url, params, key, through if sink else None)
        except _HTTPError as err:
            if err.status != 400 or "propertyName" not in params or delivered:
                raise
            del params["propertyName"]  # a field the layer doesn't have: ask for all of them
            return await self._paged(url, params, key, sink)

    async def _paged(self, url: str, params: dict[str, str], what: str,
                     sink: Callable[[list[dict]], None] | None) -> list[dict]:
        features: list[dict] = []
        start, count, got_total = 0, PAGE, 0
        while True:
            params["startIndex"] = str(start)
            params["count"] = str(count)
            try:
                page = await self._get_json(url, params, what)
            except _TooBig:
                if count <= 25:
                    raise
                count //= 2  # dense area: the same page again, in halves
                continue
            got = page.get("features") or []
            got_total += len(got)
            if sink is None:
                features += got
            else:
                await asyncio.to_thread(sink, got)
            total = page.get("numberMatched", page.get("totalFeatures"))
            del page
            if isinstance(total, int):
                if not got or got_total >= total:
                    return features
            elif len(got) < count:
                return features
            start += len(got)
            del got

    async def _fetch_mml(self, key: str, bbox: tuple[float, float, float, float],
                         sink: Callable[[Iterable[dict]], None], name: str | None = None) -> None:
        """An MML OGC API Features collection in a bbox, following its next-page links. A
        page over MAX_RESPONSE is asked for again with half the features."""
        collection = name or source_names(key)[0][1]
        lon0, lat0 = mercator_to_lonlat(bbox[0], bbox[1])
        lon1, lat1 = mercator_to_lonlat(bbox[2], bbox[3])
        limit = MML_PAGES.get(key, MML_PAGE)
        url: str = f"{MML_API}/collections/{collection}/items"
        params: dict[str, str] | None = {"bbox": f"{lon0:.6f},{lat0:.6f},{lon1:.6f},{lat1:.6f}",
                                         "limit": str(limit), "api-key": self.mml_key}
        for _ in range(5000):
            try:
                page = await self._get_json(url, params, key)
            except _TooBig:
                if limit <= 25:
                    raise
                limit //= 2
                url, params = _with_limit(url, params, limit)
                continue
            nxt = next((link.get("href") for link in page.get("links") or []
                        if link.get("rel") == "next" and link.get("href")), None)
            more = bool(page.get("features"))
            await asyncio.to_thread(lambda p=page: sink(_mml_features(p)))
            del page
            if not nxt or not more:
                break
            url, params = nxt, None
            if "api-key=" not in url:
                url += ("&" if "?" in url else "?") + f"api-key={self.mml_key}"

    # --- overview tiles (zooms 8-9) ---

    async def _overview_tile(self, z: int, x: int, y: int,
                             wait: float | None) -> tuple[dict[str, Any], bool] | None:
        key = ("overview", z, x, y)
        task = self._fetching.get(key)
        if task is None:
            task = asyncio.create_task(self._make_overview_tile(z, x, y))
            self._fetching[key] = task
            task.add_done_callback(lambda _t, key=key: self._fetching.pop(key, None))
        if not task.done():
            done, still = await asyncio.wait([asyncio.shield(task)], timeout=wait)
            if still:
                return None
        return task.result()

    async def _make_overview_tile(self, z: int, x: int, y: int) -> tuple[dict[str, Any], bool]:
        """The tile stored on disk while none of its blocks changed (or it was cut less
        than OVERVIEW_RECUT ago), else cut again from the blocks' overview levels."""
        parts = [p for p in self._parts() if p in OVERVIEW_PARTS]
        rev = self._rev()

        def scan() -> tuple[dict[tuple[int, int], list[Path]], float]:
            files: dict[tuple[int, int], list[Path]] = {}
            newest = 0.0
            for bx, by in blocks_for(z, x, y):
                paths = []
                for part in parts:
                    path = self._path(bx, by, part)
                    try:
                        newest = max(newest, path.stat().st_mtime)
                    except FileNotFoundError:
                        if not part:
                            break  # no block here yet
                        continue
                    paths.append(path)
                if paths:
                    files[(bx, by)] = paths
            return files, newest

        files, newest = await asyncio.to_thread(scan)
        have = [list(k) for k in files]
        path = self.dir / "overview" / f"{z}_{x}_{y}.json.gz"
        try:
            cached = await asyncio.to_thread(_read_json, path)
        except (OSError, ValueError, EOFError):
            cached = None
        if cached is not None and cached.get("rev") == rev:
            fresh = cached.get("newest", 0) >= newest and cached.get("have") == have \
                and cached.get("foreign", True)  # a depth model that couldn't be had is asked for again
            if fresh or time.time() - cached.get("made", 0) < OVERVIEW_RECUT:
                return cached["tile"], cached["complete"] and fresh
        # Blocks that may hold Finnish data, and how many of them are there.
        near = await asyncio.to_thread(lambda: [k for k in blocks_for(z, x, y) if near_finland(block_bounds(*k))])
        got = sum(k in files for k in near)
        async with self._cutting:
            tile = await asyncio.to_thread(make_tile, z, x, y, overview_blocks(files))
        if near:
            tile["cover"] = round(got / len(near), 2)
        complete = got == len(near)
        # Outside Finland the overview is drawn straight from the depth model.
        from .foreign import add_overview
        foreign, foreign_ok = await self.foreign.overview(z, x, y)
        add_overview(tile, foreign)
        complete = complete and foreign_ok
        if files or foreign:
            try:
                await asyncio.to_thread(_write_json, path, {"rev": rev, "newest": newest, "have": have,
                                                            "made": time.time(), "tile": tile,
                                                            "complete": complete, "foreign": foreign_ok})
            except OSError as err:
                _LOGGER.warning("Could not store chart overview tile %s/%s/%s: %s", z, x, y, err)
        return tile, complete

    # --- heritage sites ---

    @staticmethod
    def _region(bx: int, by: int) -> tuple[int, int]:
        s = BLOCK_Z - HERITAGE_Z
        return bx >> s, by >> s

    def _ensure_heritage_task(self, rx: int, ry: int) -> "asyncio.Future[bool]":
        if (rx, ry) in self._regions:
            self._regions.move_to_end((rx, ry))
            future = asyncio.get_running_loop().create_future()
            future.set_result(True)
            return future
        key = ("heritage", rx, ry)
        task = self._fetching.get(key)
        if task is None:
            task = asyncio.create_task(self._load_region(rx, ry))
            self._fetching[key] = task
            task.add_done_callback(lambda _t, key=key: self._fetching.pop(key, None))
        return task

    async def _ensure_heritage(self, bx: int, by: int) -> bool:
        return await asyncio.shield(self._ensure_heritage_task(*self._region(bx, by)))

    async def _load_region(self, rx: int, ry: int) -> bool:
        """Heritage sites of a zoom-7 region into memory, fetched once a month."""
        path = self.dir / "heritage" / f"{rx}_{ry}.v2.json.gz"  # v2 adds coastal defences
        age = _age(path)
        rows = None
        failed = time.time() - self._failed.get(("heritage", rx, ry), 0) < FAILED_RETRY
        if (age is None or age >= MAX_AGE) and not failed:
            rows = await self._fetch_region(rx, ry)
            if rows is None:
                self._failed[("heritage", rx, ry)] = time.time()
            else:
                try:
                    await asyncio.to_thread(_write_json, path, {"v": 2, "rows": rows})
                except OSError as err:
                    _LOGGER.warning("Could not store heritage sites: %s", err)
        if rows is None and age is not None:
            try:
                rows = (await asyncio.to_thread(_read_json, path))["rows"]
            except (OSError, ValueError, KeyError):
                rows = None
        if rows is None:
            return False
        b = LayerBuilder("point")
        for x, y, *props in rows:
            b.point(props, x, y)
        self._regions[(rx, ry)] = b.layer
        while len(self._regions) > HERITAGE_REGIONS:
            self._regions.popitem(last=False)
        return True

    async def _fetch_region(self, rx: int, ry: int) -> list | None:
        minx, miny, maxx, maxy = _tile_mercator(HERITAGE_Z, rx, ry)
        if not near_finland((minx, miny, maxx, maxy)):
            return []
        bbox = (minx, miny, maxx, maxy)
        async with self._limit:
            try:
                if self.fetch_layer is not None:
                    features = await self.fetch_layer("heritage", bbox)
                else:
                    features = await self._fetch_heritage(bbox)
                self.errors.pop("heritage", None)
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, KeyError) as err:
                msg = str(err) or type(err).__name__
                if self.errors.get("heritage") != msg:
                    _LOGGER.warning("Heritage sites for region %s,%s failed: %s", rx, ry, msg)
                self.errors["heritage"] = msg
                return None
        rows = []
        for f in features:
            props = heritage_props(f)
            pts = _points(f.get("geometry") or {"type": ""})
            if props and pts:
                rows.append([round(pts[0][0]), round(pts[0][1]), *props])
        _LOGGER.info("Heritage sites for region %s,%s: %s", rx, ry, len(rows))
        return rows

    async def _fetch_heritage(self, bbox: tuple[float, float, float, float]) -> list[dict]:
        """Underwater sites, wrecks and defence sites from each of Museovirasto's register
        layers. Defences come for land too; the tiles keep only those near the sea."""
        x0, y0, x1, y1 = bbox
        cql = (f"(vedenalainen ILIKE 'k' OR tyyppi ILIKE '%hyly%' OR tyyppi ILIKE '%puolustus%') AND "
               f"BBOX(Shape,{x0:.1f},{y0:.1f},{x1:.1f},{y1:.1f},'EPSG:3857')")
        features: list[dict] = []
        failed: list[str] = []
        for type_name, cls in MV_TYPES.items():
            params = {"service": "WFS", "version": "2.0.0", "request": "GetFeature", "typeNames": type_name,
                      "srsName": "EPSG:3857", "outputFormat": "application/json", "sortBy": "OBJECTID",
                      "propertyName": ",".join(("Shape", *MV_FIELDS)), "CQL_FILTER": cql}
            try:
                try:
                    got = await self._paged(MV_WFS, params, "heritage", None)
                except aiohttp.ClientError as err:
                    if not str(err).startswith("HTTP"):
                        raise
                    # A layer without one of the fields refuses the field list: ask for everything.
                    del params["propertyName"]
                    got = await self._paged(MV_WFS, params, "heritage", None)
            except (aiohttp.ClientError, ValueError) as err:
                failed.append(f"{type_name.split(':')[-1]}: {err}")
                continue
            for f in got:
                f.setdefault("properties", {})["_class"] = cls
                features.append(f)
        if failed:
            if len(failed) == len(MV_TYPES):
                raise aiohttp.ClientError("; ".join(failed))
            _LOGGER.info("Some heritage layers were left out: %s", "; ".join(failed))
        return features

    async def _coastal_sites(self, key: tuple[int, int], data: dict) -> Layer:
        """A block's heritage sites with inland defences left out, worked out once per block
        (and again when its region is reloaded)."""
        region = self._regions.get(self._region(*key))
        hit = self._coast.get(key)
        if hit is not None and hit[0] is region:
            self._coast.move_to_end(key)
            return hit[1]
        layer = await asyncio.to_thread(coastal_filter, self._heritage_layer(*key), data.get("depth"))
        self._coast[key] = (region, layer)
        while len(self._coast) > MEMORY_BLOCKS:
            self._coast.popitem(last=False)
        return layer

    def _heritage_layer(self, bx: int, by: int) -> Layer:
        """A block's heritage sites (block units, with the usual point margin)."""
        region = self._regions.get(self._region(bx, by))
        b = LayerBuilder("point")
        if region is None:
            return b.layer
        minx, miny, maxx, maxy = block_bounds(bx, by)
        scale = UNITS / (maxx - minx)
        m = BLOCK_POINT_MARGIN
        co = region.co
        for i in range(region.n):
            x, y = (co[2 * i] - minx) * scale, (maxy - co[2 * i + 1]) * scale
            if -m <= x <= UNITS + m and -m <= y <= UNITS + m:
                b.point(region.props[region.pi[i]], x, y)
        return b.layer

    def prune(self) -> None:
        """Drop blocks nobody has looked at for two refresh periods."""
        for root in (self.dir / "blocks", self.dir / "heritage", self.dir / "overview"):
            if not root.exists():
                continue
            for p in root.iterdir():
                try:
                    if time.time() - p.stat().st_mtime > 2 * MAX_AGE:
                        p.unlink()
                except FileNotFoundError:
                    pass


class _TooBig(ValueError):
    pass


class _HTTPError(aiohttp.ClientError):
    """A service answered with an error; ``text`` is the start of what it said."""

    def __init__(self, status: int, text: str):
        super().__init__(f"HTTP {status}: {text[:200]}")
        self.status = status
        self.text = text


def _fetch_again(meta: dict, name: str) -> bool:
    """Whether a block part file is to be fetched again soon: a layer failed when it was
    fetched, or the file is older than its part's PART_REV."""
    bits = name.split(".")
    part = bits[1] if len(bits) == 3 else ""
    return bool(meta.get("missing")) or meta.get("rev", 0) < PART_REV.get(part, 0)


def _age(path: Path) -> float | None:
    try:
        return time.time() - path.stat().st_mtime
    except FileNotFoundError:
        return None


def _read_json(path: Path) -> dict:
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return json.load(f)


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=5) as f:
        json.dump(data, f, separators=(",", ":"), ensure_ascii=False)
    os.replace(tmp, path)
