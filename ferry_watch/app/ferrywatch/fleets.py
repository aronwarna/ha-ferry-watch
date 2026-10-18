"""Fleets: Tallink Silja, Viking Line, Finnlines and Finferries ships, plus any ship the
user chose to follow, shown on the map with their current voyage.

Membership is worked out once a day from Digitraffic's vessel list by AIS name, so a
new ship joins as soon as Finnish AIS stations have seen it. A sold ship drops out
when it is renamed; one that keeps its name but sails elsewhere just stops reporting,
and the panel hides it after a week.
"""

from __future__ import annotations

import hashlib
import json
import time
import unicodedata
from typing import Any

from .store import Store

FLEETS: list[dict[str, Any]] = [
    {
        "id": "tallink", "name": "Tallink Silja", "color": "#7c3aed",
        "names": ["MEGASTAR", "MYSTAR", "SILJA SERENADE", "SILJA SYMPHONY", "SILJA EUROPA",
                  "BALTIC PRINCESS", "BALTIC QUEEN", "VICTORIA I", "GALAXY I"],
    },
    {
        "id": "viking", "name": "Viking Line", "color": "#dc2626",
        "names": ["VIKING GRACE", "VIKING GLORY", "VIKING XPRS", "VIKING CINDERELLA", "GABRIELLA",
                  "AMORELLA"],
    },
    {
        "id": "finnlines", "name": "Finnlines", "color": "#0284c7",
        "names": ["EUROPALINK", "NORDLINK"],
        # Finnlines names its ships Finn-something; the type check keeps tugs and boats out.
        "prefixes": ["FINN"],
    },
    {
        # Archipelago and road ferries; Utö, Kökar and Tuuli sail the Utö route. Names
        # like Stella are common, so these must also report a passenger or cargo type.
        "id": "finferries", "name": "Finferries", "color": "#0f766e",
        "names": ["UTO", "KOKAR", "TUULI", "ALTERA", "ELEKTRA", "STELLA", "MERGUS", "FINNO"],
        "typed": True,
    },
]
FOLLOWED = {"id": "followed", "name": "Followed ships", "color": "#475569"}
FLEET_IDS = [f["id"] for f in FLEETS] + [FOLLOWED["id"]]
# Changing the fleet lists above re-reads the vessel list at the next start.
FLEET_DEFS = hashlib.sha1(json.dumps(FLEETS, sort_keys=True).encode()).hexdigest()[:12]
PREFIX_SHIP_TYPES = range(60, 80)  # AIS passenger (60-69) and cargo (70-79)
UPPERCASE_WORDS = {"XPRS", "II", "III"}
SPECIAL_NAMES = {"MYSTAR": "MyStar", "UTO": "Utö", "KOKAR": "Kökar", "FINNO": "Finnö"}


def normalize(name: str) -> str:
    """AIS-style name: upper case ASCII, single spaces (Utö -> UTO)."""
    folded = unicodedata.normalize("NFKD", name.replace("@", " ")).encode("ascii", "ignore").decode()
    return " ".join(folded.upper().split())


def display_name(ais_name: str) -> str:
    if ais_name in SPECIAL_NAMES:
        return SPECIAL_NAMES[ais_name]
    return " ".join(w if w in UPPERCASE_WORDS else w.capitalize() for w in ais_name.split())


def fleet_for(name: str, ship_type: Any) -> str | None:
    for fleet in FLEETS:
        if name in fleet["names"] and (not fleet.get("typed") or ship_type in PREFIX_SHIP_TYPES):
            return fleet["id"]
        if any(name.startswith(p) for p in fleet.get("prefixes", ())) and ship_type in PREFIX_SHIP_TYPES:
            return fleet["id"]
    return None


def match_fleets(vessels: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    """{mmsi: {"fleet", "name"}} from Digitraffic vessel metadata. When a name has several
    MMSIs (a re-flag), the most recently updated one wins."""
    best: dict[str, tuple[int, int, str]] = {}  # name -> (updated ms, mmsi, fleet)
    for v in vessels:
        name = normalize(str(v.get("name") or ""))
        updated = v.get("timestamp") or 0
        try:
            mmsi = int(v["mmsi"])
        except (KeyError, TypeError, ValueError):
            continue
        if not name:
            continue
        fleet = fleet_for(name, v.get("shipType"))
        if fleet and (name not in best or updated > best[name][0]):
            best[name] = (updated, mmsi, fleet)
    return {mmsi: {"fleet": fleet, "name": display_name(name)} for name, (_, mmsi, fleet) in best.items()}


class FleetManager:
    def __init__(self, store: Store):
        self.store = store
        self.matched: dict[int, dict[str, Any]] = {
            int(m): info for m, info in store.kv_get("fleet_members", {}).items()
        }
        self.followed: dict[int, str] = {int(m): n for m, n in store.kv_get("followed_ships", {}).items()}
        enabled = store.kv_get("fleets_enabled")
        if enabled is None:
            self.enabled = set(FLEET_IDS)
        else:
            self.enabled = set(enabled)
            # Fleets added in an update start switched on.
            known = set(store.kv_get("fleets_known", ["tallink", "viking", "finnlines"]))
            self.enabled |= set(FLEET_IDS) - known
            store.kv_set("fleets_enabled", sorted(self.enabled))
        store.kv_set("fleets_known", FLEET_IDS)
        self.updated_ts: float = store.kv_get("fleet_members_ts", 0)
        if store.kv_get("fleet_defs") != FLEET_DEFS:
            self.updated_ts = 0  # the lists changed; match again now

    @property
    def members(self) -> dict[int, dict[str, Any]]:
        followed = {m: {"fleet": FOLLOWED["id"], "name": n} for m, n in self.followed.items()}
        return {**self.matched, **followed}

    def update(self, members: dict[int, dict[str, Any]]) -> bool:
        """Replace the matched member list; returns whether it changed."""
        self.updated_ts = time.time()
        self.store.kv_set("fleet_members_ts", self.updated_ts)
        self.store.kv_set("fleet_defs", FLEET_DEFS)
        if not members or members == self.matched:
            return False  # an empty answer is more likely a fault than every fleet gone
        self.matched = members
        self.store.kv_set("fleet_members", {str(m): info for m, info in members.items()})
        return True

    def follow(self, mmsi: int, name: str) -> None:
        self.followed[mmsi] = name
        self._save_followed()

    def unfollow(self, mmsi: int) -> None:
        self.followed.pop(mmsi, None)
        self._save_followed()

    def _save_followed(self) -> None:
        self.store.kv_set("followed_ships", {str(m): n for m, n in self.followed.items()})

    def set_enabled(self, fleet_id: str, on: bool) -> None:
        if on:
            self.enabled.add(fleet_id)
        else:
            self.enabled.discard(fleet_id)
        self.store.kv_set("fleets_enabled", sorted(self.enabled))

    def color(self, fleet_id: str) -> str:
        return next((f["color"] for f in [*FLEETS, FOLLOWED] if f["id"] == fleet_id), "#64748b")

    def active(self, tracked: set[int]) -> dict[int, dict[str, Any]]:
        """Members of the switched-on fleets that the main tracker doesn't already follow."""
        return {m: info for m, info in self.members.items()
                if info["fleet"] in self.enabled and m not in tracked}

    def summary(self) -> list[dict[str, Any]]:
        members = self.members
        return [
            {
                "id": f["id"], "name": f["name"], "color": f["color"], "enabled": f["id"] in self.enabled,
                "members": sorted(m for m, info in members.items() if info["fleet"] == f["id"]),
            }
            for f in [*FLEETS, FOLLOWED]
        ]
