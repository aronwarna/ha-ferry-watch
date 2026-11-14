"""Made-up nautical warnings, sea weather warnings, bridges, locks and speed limits round
Porkkala for the simulator (FW_SIMULATE=1), in the shapes notices.py reads."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from .notices import RESTRICT_TYPE, STRUCTURES, _merc


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def nautical() -> dict[str, Any]:
    now = time.time()
    def feature(i, geometry, kind, area, place, text, days=3):
        return {"type": "Feature", "geometry": geometry, "properties": {
            "id": 1000 + i, "number": 40 + i, "typeEn": kind, "typeFi": kind, "areasEn": area, "locationEn": place,
            "contentsEn": text, "contentsFi": text, "publishingTime": _iso(now - i * 7200),
            "validityStartTime": _iso(now - 3600), "validityEndTime": _iso(now + days * 86400) if days else None}}
    return {"type": "FeatureCollection", "features": [
        feature(1, {"type": "Polygon", "coordinates": [[[24.20, 59.86], [24.42, 59.86], [24.42, 59.93], [24.20, 59.93], [24.20, 59.86]]]},
                "LOCAL WARNING", "GULF OF FINLAND", "SOUTH OF PORKKALA",
                "FIRING EXERCISES IN PROGRESS DAILY 0800-1600 LT. DANGER AREA BOUNDED BY THE POSITIONS GIVEN. WIDE BERTH REQUESTED."),
        feature(2, {"type": "Point", "coordinates": [24.4530, 59.9590]},
                "LOCAL WARNING", "GULF OF FINLAND", "PORKKALA INNER FAIRWAY",
                "SPAR BUOY UTTERBOTE NR 3 IS OFF STATION.", days=0),
        feature(3, {"type": "LineString", "coordinates": [[24.50, 60.02], [24.56, 60.035], [24.62, 60.03]]},
                "BOATING WARNING", "GULF OF FINLAND", "KIRKKONUMMI ARCHIPELAGO",
                "UNDERWATER CABLE WORKS. A WORK VESSEL WITH DIVERS ALONG THE LINE. PASS AT LOW SPEED.", days=10),
    ]}


def cap() -> bytes:
    now = time.time()
    onset, expires = now + 2 * 3600, now + 11 * 3600
    def iso(ts):
        return datetime.fromtimestamp(ts, timezone.utc).astimezone().isoformat(timespec="seconds")
    def alert(ident, code, event, headline, text, color, polygon, area, extra=""):
        return f"""<entry><title>{headline}</title><content type="text/xml">
<alert xmlns="urn:oasis:names:tc:emergency:cap:1.2"><identifier>{ident}</identifier>
<info><language>fi-FI</language><event>Tuulivaroitus merialueille</event>
<eventCode><valueName>profile</valueName><value>{code}</value></eventCode></info>
<info><language>en-GB</language><category>Met</category><event>{event}</event>
<urgency>Future</urgency><severity>Moderate</severity><certainty>Possible</certainty>
<eventCode><valueName>profile:cap:https://alerts.fmi.fi/cap/profile/v1.1.0</valueName><value>{code}</value></eventCode>
<onset>{iso(onset)}</onset><expires>{iso(expires)}</expires>
<headline>{headline}</headline><description>{text}</description>{extra}
<parameter><valueName>color</valueName><value>{color}</value></parameter>
<area><areaDesc>{area}</areaDesc><polygon>{polygon}</polygon><geocode><valueName>METAREA</valueName><value>B5W</value></geocode></area>
</info></alert></content></entry>"""
    west = "59.3,22.9 60.25,22.9 60.25,25.3 59.6,25.3 59.3,24.0 59.3,22.9"
    east = "59.6,25.3 60.25,25.3 60.6,27.9 59.9,27.9 59.6,25.3"
    wind = "<parameter><valueName>windIntensity</valueName><value>15</value></parameter>" \
           "<parameter><valueName>windDirection</valueName><value>225</value></parameter>"
    body = f"""<?xml version="1.0" encoding="UTF-8"?><feed xmlns="http://www.w3.org/2005/Atom">
<title>Finnish Meteorological Institute warnings</title>
{alert("sim-1", "seaWind", "Wind warning for sea areas", "Yellow wind warning for sea area: Western Gulf of Finland",
       "Near gale warning: Southwest wind 15 m/s in the evening.", "yellow", west, "Western Gulf of Finland", wind)}
{alert("sim-2", "seaWaveHeight", "Wave height warning", "Orange wave height warning: Eastern Gulf of Finland",
       "Significant wave height 2.5 m in the night.", "orange", east, "Eastern Gulf of Finland")}
{alert("sim-3", "forestFireWeather", "Wildfire warning", "Yellow wildfire warning: Uusimaa", "A wildfire warning is in force.",
       "yellow", "60.0,24.0 60.5,24.0 60.5,25.0 60.0,24.0", "Uusimaa")}
</feed>"""
    return body.encode()


def wfs(type_name: str, bbox: tuple[float, float, float, float] | None = None) -> dict[str, Any]:
    def pt(lat, lon):
        return list(_merc(lat, lon))
    def feature(geometry, **props):
        return {"type": "Feature", "geometry": geometry, "properties": props}
    if type_name == STRUCTURES["bridge"]:
        return {"features": [feature({"type": "Point", "coordinates": pt(60.045, 24.415)}, nimi="Simsalmen läppäsilta",
                                     alikulkukorkeus_suljettuna=3.2, leveys=9.0, tyyppi="Läppäsilta", OBJECTID=1)]}
    if type_name == STRUCTURES["lock"]:
        return {"features": [feature({"type": "Point", "coordinates": pt(60.062, 24.36)}, nimi="Simkosken sulku",
                                     pituus=35.0, leveys=8.2, kynnyssyvyys=2.4, kayttotapa="Itsepalvelu", OBJECTID=2)]}
    if type_name == STRUCTURES["canal"]:
        return {"features": [feature({"type": "LineString", "coordinates": [pt(60.058, 24.35), pt(60.062, 24.36), pt(60.067, 24.365)]},
                                     nimi="Simkosken kanava", kulkusyvyys=2.4, alikulkukorkeus=12.0, OBJECTID=3)]}
    if type_name == RESTRICT_TYPE:
        ring = [pt(59.985, 24.47), pt(59.985, 24.55), pt(60.02, 24.55), pt(60.02, 24.47), pt(59.985, 24.47)]
        small = [pt(60.03, 24.40), pt(60.03, 24.44), pt(60.05, 24.44), pt(60.05, 24.40), pt(60.03, 24.40)]
        return {"features": [
            feature({"type": "Polygon", "coordinates": [ring]}, rajoitustyypit="01", suuruus=10),
            feature({"type": "MultiPolygon", "coordinates": [[small]]}, rajoitustyypit="01, 02", suuruus=30),
            feature({"type": "Polygon", "coordinates": [ring]}, rajoitustyypit="03", suuruus=None),
        ]}
    return {"features": []}


FAKE = {"nautical": nautical, "cap": cap, "wfs": wfs}
