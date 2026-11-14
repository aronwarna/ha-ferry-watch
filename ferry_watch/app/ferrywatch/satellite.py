"""Sharper aerial photos for the Satellite view, fetched and kept by the NAS.

Esri's World Imagery stays underneath everywhere (the panel loads it straight from
Esri). On top of it, where a country publishes its own aerial photos for free, the
panel asks the add-on for those:

- Finland (incl. Åland): the Land Survey's orthophotos (0.5 m per pixel, summer,
  renewed every three years). Kapsi's free WMS serves them at full resolution with
  no key; the Land Survey's own open WMTS (with the user's API key) only goes to
  zoom 16 (about 1.2 m per pixel here), so it is the fallback when Kapsi is down.
- Estonia: Maa-amet's orthophotos (TMS, up to zoom 18).

Elsewhere the add-on answers 404 at once and Esri shows. The photos come with
transparency where a service has no picture (out at sea, over the border), so
Esri shows through there too.
"""

from __future__ import annotations

import math
from pathlib import Path

from .charts import FINNISH_WATERS, _inside, _tile_lat, _tile_lon
from .tilecache import TileCache

MIN_Z = 11           # further out Esri's own imagery is as good
MAX_Z = 18           # the panel scales zoom 19 up from these
MML_WMTS_MAX_Z = 16  # the Land Survey's open WMTS stops here

KAPSI_WMS = ("https://tiles.kartat.kapsi.fi/ortokuva?SERVICE=WMS&VERSION=1.1.1&REQUEST=GetMap"
             "&LAYERS=ortokuva&STYLES=&SRS=EPSG:3857&BBOX={bbox}&WIDTH=256&HEIGHT=256"
             "&FORMAT=image/vnd.jpeg-png&TRANSPARENT=TRUE")
MML_WMTS = ("https://avoin-karttakuva.maanmittauslaitos.fi/avoin/wmts/1.0.0/ortokuva/default/"
            "WGS84_Pseudo-Mercator/{z}/{y}/{x}.jpg?api-key={key}")
MAAAMET_TMS = "https://tiles.maaamet.ee/tm/tms/1.0.0/foto@GMC/{z}/{x}/{ty}.png"

# Estonia and its waters, roughly: south of Finland's sea border (FINNISH_WATERS'
# southern edge), east to Narva and Lake Peipus. (longitude, latitude)
ESTONIA = (
    (21.0, 57.5), (21.0, 59.4), (22.3, 59.47), (23.4, 59.57), (24.4, 59.69), (25.3, 59.82),
    (26.2, 59.89), (26.7, 59.95), (27.4, 59.55), (28.25, 59.4), (27.9, 58.9), (27.6, 58.0),
    (27.5, 57.5),
)

ATTRIBUTION = {
    "fi": "Aerial photos © Maanmittauslaitos (via Kapsi)",
    "ee": "Aerial photos © Maa-amet",
}

HALF = 20037508.342789244


def _points(z: int, x: int, y: int) -> list[tuple[float, float]]:
    west, east = _tile_lon(x, z), _tile_lon(x + 1, z)
    north, south = _tile_lat(y, z), _tile_lat(y + 1, z)
    mid_lon, mid_lat = (west + east) / 2, _tile_lat(y + 0.5, z)
    return [(mid_lon, mid_lat), (west, north), (east, north), (west, south), (east, south)]


def region(z: int, x: int, y: int) -> str | None:
    """"fi", "ee" or None: whose photos cover this web-mercator tile. A tile on the
    border goes to the country holding its middle, else any corner."""
    if not (MIN_Z <= z <= MAX_Z and 0 <= x < 2**z and 0 <= y < 2**z):
        return None
    pts = _points(z, x, y)
    for test in (pts[:1], pts[1:]):
        for lon, lat in test:
            if _inside(lon, lat, FINNISH_WATERS):
                return "fi"
            if _inside(lon, lat, ESTONIA):
                return "ee"
    return None


def mercator_bbox(z: int, x: int, y: int) -> str:
    size = 2 * HALF / 2**z
    x0, y1 = -HALF + x * size, HALF - y * size
    return f"{x0:.3f},{y1 - size:.3f},{x0 + size:.3f},{y1:.3f}"


class Satellite:
    def __init__(self, cache: TileCache, mml_key: str = ""):
        self.cache = cache
        self.mml_key = mml_key

    async def tile(self, z: int, x: int, y: int) -> Path | None:
        """The aerial photo for this tile on disk, or None (Esri shows instead)."""
        where = region(z, x, y)
        if where == "ee":
            ty = 2**z - 1 - y
            return await self.cache.get("ee", z, x, y, MAAAMET_TMS.format(z=z, x=x, ty=ty))
        if where != "fi":
            return None
        path = await self.cache.get("fi", z, x, y, KAPSI_WMS.format(bbox=mercator_bbox(z, x, y)))
        if path or "fi" not in self.cache.failing:
            return path
        # Kapsi didn't answer: the Land Survey's own (coarser) tiles, if there's a key.
        if self.mml_key and z <= MML_WMTS_MAX_Z:
            return await self.cache.get("fi-mml", z, x, y, MML_WMTS.format(z=z, x=x, y=y, key=self.mml_key))
        return None

    def sources(self) -> list[dict]:
        """Settings' rows: which photo services are failing right now."""
        names = {"fi": "Aerial photos Finland (Kapsi)", "fi-mml": "Aerial photos Finland (Land Survey)",
                 "ee": "Aerial photos Estonia (Maa-amet)"}
        return [{"name": names[k], "failing": self.cache.failing.get(k)} for k in names]
