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
SUPERVISOR_API = "http://supervisor"
# FW_STANDALONE=1: running as a plain container, outside Home Assistant (set by
# Dockerfile.standalone). Home Assistant is then optional and reached over the network
# with FW_HA_URL (like http://192.168.1.30:8123) and FW_HA_TOKEN (a long-lived access
# token), and the panel is served to whoever can reach the port: keep it on a private
# network, or set FW_PASSWORD to ask for one.
STANDALONE = os.environ.get("FW_STANDALONE") == "1" and not SUPERVISOR_TOKEN
HA_URL = os.environ.get("FW_HA_URL", "").rstrip("/")
HA_TOKEN = SUPERVISOR_TOKEN or (os.environ.get("FW_HA_TOKEN") if STANDALONE and HA_URL else None)
HA_API = f"{HA_URL}/api" if STANDALONE and HA_URL else "http://supervisor/core/api"
PASSWORD = os.environ.get("FW_PASSWORD") or None
# Where phones reach the panel from outside (like http://192.168.1.20:8099): what a tap
# on a notification opens, and where its picture is loaded from. Standalone only.
PUBLIC_URL = os.environ.get("FW_PUBLIC_URL", "").rstrip("/") if STANDALONE else ""
# A second way to send alerts, used when Home Assistant doesn't answer or isn't set up:
# an ntfy topic address, like https://ntfy.sh/my-secret-topic.
NTFY_URL = os.environ.get("FW_NTFY_URL", "").rstrip("/") or None
# Notification pictures go to HA's media folder, which the companion app can load
# (standalone: a folder of the app's own, served by the app).
MEDIA_DIR = Path(os.environ.get("FW_MEDIA_DIR", str(DATA_DIR / "media") if STANDALONE else "/media")) / "ferry_watch"

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
        return env_options(opts)
    except (OSError, ValueError) as err:
        logging.getLogger(__name__).warning("Could not read %s: %s", path, err)
        return opts
    for f in fields(Options):
        if f.name in raw:
            setattr(opts, f.name, raw[f.name])
    return env_options(opts)


def env_options(opts: Options, env: dict[str, str] | None = None) -> Options:
    """Options from the environment, for the standalone container, which has no add-on
    Configuration tab: FW_NOTIFY_SERVICE, FW_RETENTION_DAYS, FW_REQUIRE_PHONE_AREA and so
    on, one for each option, winning over options.json."""
    env = os.environ if env is None else env
    for f in fields(Options):
        value = env.get(f"FW_{f.name.upper()}")
        if value is None or value == "":
            continue
        default = getattr(Options, f.name)
        try:
            if isinstance(default, bool):
                setattr(opts, f.name, value.strip().lower() in ("1", "true", "yes", "on"))
            elif isinstance(default, int):
                setattr(opts, f.name, int(value))
            else:
                setattr(opts, f.name, value)
        except ValueError:
            logging.getLogger(__name__).warning("FW_%s=%r is not a number; left at %r",
                                                f.name.upper(), value, getattr(opts, f.name))
    return opts
