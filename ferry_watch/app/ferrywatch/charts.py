"""Finds a web-mercator nautical chart layer in Traficom's open WMTS service.

The service's layer names aren't documented in a stable place, so the add-on
reads the capabilities document itself and picks a chart layer that Leaflet
can show. If none is found, the panel falls back to OpenStreetMap with
OpenSeaMap sea marks.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from urllib.parse import quote
import xml.etree.ElementTree as ET
from typing import Any

import aiohttp

from .config import USER_AGENT
from .store import Store

_LOGGER = logging.getLogger(__name__)

CAPABILITIES_URL = "https://julkinen.traficom.fi/rasteripalvelu/wmts?request=GetCapabilities&service=WMTS"
CACHE_KEY = "chart_layers"
CACHE_TTL = 7 * 24 * 3600
FAILED_TTL = 6 * 3600
ATTRIBUTION = "Nautical chart © Traficom, not for navigation"

NS = {
    "wmts": "http://www.opengis.net/wmts/1.0",
    "ows": "http://www.opengis.net/ows/1.1",
}
MERCATOR_CRS = ("3857", "900913", "3785")
CHART_WORDS = ("merikartta", "nautical", "chart", "sjökort", "rannikko", "coastal")
# Traficom splits its charts into series (A east of Helsinki, B west of it, and so on).
# These are stacked bottom to top: overview charts for open water, then all the series.
PREFERRED = ("yleiskartat 250k public", "yleiskartat 100k public", "merikarttasarjat public")


def _plain(ident: str) -> str:
    return ident.split(":", 1)[-1].strip().lower()


def _text(el: ET.Element | None) -> str:
    return (el.text or "").strip() if el is not None else ""


def parse_capabilities(xml: str | bytes) -> dict[str, Any] | None:
    root = ET.fromstring(xml)
    contents = root.find("wmts:Contents", NS)
    if contents is None:
        return None

    # Tile matrix sets in web mercator, with their matrix ids ordered by zoom.
    mercator: dict[str, list[str]] = {}
    for tms in contents.findall("wmts:TileMatrixSet", NS):
        ident = _text(tms.find("ows:Identifier", NS))
        crs = _text(tms.find("ows:SupportedCRS", NS))
        if not any(code in crs for code in MERCATOR_CRS):
            continue
        matrices = []
        for tm in tms.findall("wmts:TileMatrix", NS):
            width = int(_text(tm.find("wmts:MatrixWidth", NS)) or 0)
            matrices.append((width, _text(tm.find("ows:Identifier", NS))))
        matrices.sort()
        # Zoom level z has 2**z tiles across; map each matrix to its zoom.
        by_zoom: dict[int, str] = {}
        for width, mid in matrices:
            if width > 0 and width & (width - 1) == 0:
                by_zoom[width.bit_length() - 1] = mid
        if by_zoom:
            mercator[ident] = [by_zoom.get(z, "") for z in range(max(by_zoom) + 1)]

    if not mercator:
        return None

    get_tile = None
    for op in root.iter(f"{{{NS['ows']}}}Operation"):
        if op.get("name") == "GetTile":
            for get in op.iter(f"{{{NS['ows']}}}Get"):
                get_tile = get.get("{http://www.w3.org/1999/xlink}href")
                break

    candidates = []
    for layer in contents.findall("wmts:Layer", NS):
        ident = _text(layer.find("ows:Identifier", NS))
        title = _text(layer.find("ows:Title", NS))
        links = [_text(l.find("wmts:TileMatrixSet", NS)) for l in layer.findall("wmts:TileMatrixSetLink", NS)]
        sets = [s for s in links if s in mercator]
        if not sets:
            continue
        formats = [_text(f) for f in layer.findall("wmts:Format", NS)]
        fmt = "image/png" if "image/png" in formats else (formats[0] if formats else "image/png")
        style = ""
        for st in layer.findall("wmts:Style", NS):
            style = _text(st.find("ows:Identifier", NS))
            if st.get("isDefault") == "true":
                break
        template = None
        for res in layer.findall("wmts:ResourceURL", NS):
            if res.get("resourceType") == "tile":
                template = res.get("template")
                if res.get("format") == fmt:
                    break
        if not template and get_tile:
            sep = "" if get_tile.endswith(("?", "&")) else ("&" if "?" in get_tile else "?")
            template = (f"{get_tile}{sep}SERVICE=WMTS&REQUEST=GetTile&VERSION=1.0.0&LAYER={quote(ident)}"
                        f"&STYLE={{Style}}&FORMAT={fmt}&TILEMATRIXSET={{TileMatrixSet}}"
                        f"&TILEMATRIX={{TileMatrix}}&TILEROW={{TileRow}}&TILECOL={{TileCol}}")
        if not template:
            continue
        name = f"{ident} {title}".lower()
        score = sum(word in name for word in CHART_WORDS)
        candidates.append((score, ident, title, sets[0], template, style))

    if not candidates:
        return None
    by_name = {_plain(c[1]): c for c in candidates}
    chosen = [by_name[name] for name in PREFERRED if name in by_name]
    if not chosen:
        chosen = [max(candidates, key=lambda c: c[0])]
    layers = []
    for _, ident, title, tms_id, template, style in chosen:
        matrix_ids = mercator[tms_id]
        url = (template.replace("{TileMatrixSet}", tms_id).replace("{Style}", style)
               .replace("{TileRow}", "{y}").replace("{TileCol}", "{x}"))
        layers.append({
            "layer": ident,
            "title": title,
            "id": re.sub(r"[^a-z0-9]+", "-", _plain(ident)).strip("-"),  # also the cache folder
            "url": url,  # {TileMatrix} is filled in per zoom
            "matrix_ids": matrix_ids,
            "min_zoom": next((z for z, m in enumerate(matrix_ids) if m), 0),
            "max_zoom": len(matrix_ids) - 1,
        })
    return {"layers": layers, "attribution": ATTRIBUTION}


async def chart_layers(session: aiohttp.ClientSession, store: Store) -> dict[str, Any] | None:
    cached = store.kv_get(CACHE_KEY)
    if cached and time.time() - cached["ts"] < (CACHE_TTL if cached["chart"] else FAILED_TTL):
        return cached["chart"]
    chart = None
    try:
        async with session.get(CAPABILITIES_URL, headers={"User-Agent": USER_AGENT},
                               timeout=aiohttp.ClientTimeout(total=30)) as resp:
            resp.raise_for_status()
            chart = parse_capabilities(await resp.read())
    except (aiohttp.ClientError, asyncio.TimeoutError, ET.ParseError) as err:
        _LOGGER.warning("Could not read Traficom chart service: %s", err)
    if chart:
        _LOGGER.info("Using Traficom chart layers: %s", ", ".join(l["title"] for l in chart["layers"]))
    else:
        _LOGGER.info("No web-mercator Traficom chart layer found; using OpenSeaMap")
    store.kv_set(CACHE_KEY, {"ts": time.time(), "chart": chart})
    return chart
