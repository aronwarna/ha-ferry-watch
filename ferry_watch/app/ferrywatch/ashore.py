"""Places to go ashore along the coast, from OpenStreetMap (© OpenStreetMap contributors,
ODbL): lean-to shelters, campfire sites, wilderness and day huts, campsites, public
saunas, toilets and drinking water by the shore, nature trails' starts. The kind of
thing a boater looks for on an island, the same way harbours.py fetches harbours: one
Overpass download a month into a small gzip file, rows in memory only while asked for.

Rows: [osm id, lat, lon, name, kind, [extras]] where extras are short words
("fire", "wood", "toilet", "water", "free", "fee").
"""

from __future__ import annotations

from typing import Any

from .coast import near_sea, overpass_poly
from .harbours import TIMEOUT, Harbours

# (Overpass filter, kind) in priority order: the first that matches an element names it.
FILTERS = [
    ('"amenity"="shelter"]["shelter_type"="lean_to"', "Lean-to shelter"),
    ('"tourism"="wilderness_hut"', "Wilderness hut"),
    ('"amenity"="shelter"]["shelter_type"="basic_hut"', "Day hut"),
    ('"tourism"="camp_site"', "Campsite"),
    ('"leisure"="firepit"', "Campfire site"),
    ('"leisure"="sauna"]["access"!="private"', "Sauna"),
    ('"amenity"="toilets"]["access"!="private"', "Toilet"),
    ('"amenity"="drinking_water"', "Drinking water"),
]


def ashore_query() -> str:
    poly = overpass_poly()
    parts = "".join(f'nwr[{f}](poly:"{poly}");' for f, _ in FILTERS)
    return f"[out:json][timeout:{TIMEOUT - 30}];({parts});out center tags;"


def _kind(tags: dict[str, str]) -> str | None:
    amenity, shelter = tags.get("amenity"), tags.get("shelter_type")
    if amenity == "shelter" and shelter == "lean_to":
        return "Lean-to shelter"
    if tags.get("tourism") == "wilderness_hut":
        return "Wilderness hut"
    if amenity == "shelter" and shelter == "basic_hut":
        return "Day hut"
    if tags.get("tourism") == "camp_site":
        return "Campsite"
    if tags.get("leisure") == "firepit":
        return "Campfire site"
    if tags.get("access") == "private":
        return None
    if tags.get("leisure") == "sauna":
        return "Sauna"
    if amenity == "toilets":
        return "Toilet"
    if amenity == "drinking_water":
        return "Drinking water"
    return None


def _extras(tags: dict[str, str]) -> list[str]:
    out = []
    if tags.get("fireplace") == "yes" or tags.get("leisure") == "firepit":
        out.append("fire")
    if tags.get("firewood") == "yes" or tags.get("wood_provided") == "yes":
        out.append("wood")
    if tags.get("toilets") == "yes":
        out.append("toilet")
    if tags.get("drinking_water") == "yes":
        out.append("water")
    if tags.get("fee") == "no":
        out.append("free")
    elif tags.get("fee") == "yes":
        out.append("fee")
    return out


def parse_ashore(data: Any) -> list[list[Any]]:
    rows = []
    for el in (data or {}).get("elements") or []:
        tags = el.get("tags") or {}
        kind = _kind(tags)
        lat = el.get("lat", (el.get("center") or {}).get("lat"))
        lon = el.get("lon", (el.get("center") or {}).get("lon"))
        if not kind or lat is None or lon is None or not near_sea(lat, lon):
            continue
        name = (tags.get("name") or tags.get("name:fi") or tags.get("name:sv") or "")[:80]
        rows.append([f"{el.get('type', 'n')[:1]}{el.get('id', 0)}", round(lat, 5), round(lon, 5), name, kind,
                     _extras(tags)])
    return rows


class Ashore(Harbours):
    WHAT = "places ashore"

    def query(self) -> str:
        return ashore_query()

    def parse(self, data: Any) -> list[list[Any]]:
        return parse_ashore(data)
