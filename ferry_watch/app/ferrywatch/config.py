"""Runtime configuration: add-on options, paths and constants."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from . import __version__

DATA_DIR = Path(os.environ.get("FW_DATA_DIR", "/data"))
PORT = int(os.environ.get("FW_PORT", "8099"))
# FW_DEV=1 disables the ingress-only access check and HA publishing, for local runs.
DEV = os.environ.get("FW_DEV") == "1"
SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN")
HA_API = "http://supervisor/core/api"

USER_AGENT = f"ha-ferry-watch/{__version__} (+https://github.com/aronwarna/ha-ferry-watch)"

# The four ferries that use the Porkkala inner route. auto_resolve lets the
# feed fix an MMSI by exact name match if a ship has been re-flagged.
DEFAULT_SHIPS = [
    {"mmsi": 230184000, "name": "Silja Serenade", "color": "#2563eb", "wiki": "MS Silja Serenade"},
    {"mmsi": 265004000, "name": "Silja Symphony", "color": "#0d9488", "wiki": "MS Silja Symphony"},
    {"mmsi": 230713000, "name": "Viking Cinderella", "color": "#db2777", "wiki": "MS Viking Cinderella"},
    {"mmsi": 230361000, "name": "Viking Gabriella", "color": "#ea580c", "wiki": "MS Gabriella"},
]

# Colours handed out to ships added later, in order.
EXTRA_COLORS = ["#7c3aed", "#65a30d", "#0891b2", "#b45309", "#be123c", "#4338ca"]


@dataclass
class Options:
    retention_days: int = 60
    ha_entities: bool = True
    log_level: str = "info"
    ha_update_interval: int = 60


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
    for key in ("retention_days", "ha_entities", "log_level", "ha_update_interval"):
        if key in raw:
            setattr(opts, key, raw[key])
    return opts
