"""Runtime configuration: add-on options, paths and constants."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, fields
from pathlib import Path

from . import __version__

DATA_DIR = Path(os.environ.get("FW_DATA_DIR", "/data"))
PORT = int(os.environ.get("FW_PORT", "8099"))
# FW_DEV=1 disables the ingress-only access check and HA publishing, for local runs.
DEV = os.environ.get("FW_DEV") == "1"
SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN")
HA_API = "http://supervisor/core/api"
SUPERVISOR_API = "http://supervisor"
# Notification pictures go to HA's media folder, which the companion app can load.
MEDIA_DIR = Path(os.environ.get("FW_MEDIA_DIR", "/media")) / "ferry_watch"

USER_AGENT = f"ha-ferry-watch/{__version__} (+https://github.com/aronwarna/ha-ferry-watch)"

# The four ferries that use the Porkkala inner route. auto_resolve lets the
# feed fix an MMSI by exact name match if a ship has been re-flagged.
DEFAULT_SHIPS = [
    {"mmsi": 230184000, "name": "Silja Serenade", "color": "#2563eb", "wiki": "MS Silja Serenade"},
    {"mmsi": 265004000, "name": "Silja Symphony", "color": "#0d9488", "wiki": "MS Silja Symphony"},
    {"mmsi": 230713000, "name": "Viking Cinderella", "color": "#db2777", "wiki": "MS Viking Cinderella"},
    {"mmsi": 230361000, "name": "Viking Gabriella", "color": "#ea580c", "wiki": "MS Gabriella"},
]

# First drafts of the zones, copied from the original YAML setup so detection
# works out of the box. Points are [lat, lon]. Replace them in the area editor.
DEFAULT_ZONES = [
    {
        "name": "Inner route (from old setup)", "role": "inner", "shape": "polygon",
        "points": [[60.095422, 24.923673], [59.869197, 24.303396], [59.796673, 23.608556],
                   [60.123167, 24.434314], [60.104759, 24.918643]],
    },
    {
        "name": "My area (from old setup)", "role": "phone", "shape": "polygon",
        "points": [[60.1700215, 24.9587688], [60.1598556, 24.8765052], [60.1699175, 24.8186078],
                   [60.1455416, 24.6693898], [60.1305485, 24.4288562], [60.0799264, 24.1265156],
                   [60.0430448, 23.8854389], [59.8100256, 23.5815561], [59.8322057, 24.4000159],
                   [60.095708, 25.1552299]],
    },
]

# Colours handed out to ships added later, in order.
EXTRA_COLORS = ["#7c3aed", "#65a30d", "#0891b2", "#b45309", "#be123c", "#4338ca"]


@dataclass
class Options:
    retention_days: int = 60
    ha_entities: bool = True
    log_level: str = "info"
    ha_update_interval: int = 60
    notify_service: str = "notify.mobile_app_samsung_galaxy_s22"
    phone_entity: str = "device_tracker.samsung_galaxy_s22"
    phone_max_age_minutes: int = 15
    require_phone_area: bool = True
    notify_feed_problems: bool = True


def load_options() -> Options:
    opts = Options()
    path = DATA_DIR / "options.json"
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError:
        return opts
    except (OSError, ValueError) as err:
        logging.getLogger(__name__).warning("Could not read %s: %s", path, err)
        return opts
    for f in fields(Options):
        if f.name in raw:
            setattr(opts, f.name, raw[f.name])
    return opts
