# Ferry Watch

Ferry Watch follows the Silja Serenade, Silja Symphony, Viking Cinderella and
Viking Gabriella live, and shows them in its own panel in the Home Assistant
sidebar.

## What it does

- **Live positions.** One open connection to Digitraffic's live AIS stream.
  Positions arrive as the ships send them, with no polling.
- **Tracks since last port.** A stop of more than 20 minutes counts as a port
  call. Each ship's track starts when it leaves port. Tap a ship to see its
  whole current voyage.
- **Map, satellite or nautical chart.** Switch with the buttons at the top.
  The chart is Traficom's open nautical chart when the add-on can find it,
  otherwise a street map with OpenSeaMap sea marks. Not for navigation.
- **Ship photos.** Each ship's photo comes from its Wikipedia article. Use
  **Change photo** on a ship's card to upload your own.
- **Other ships.** Add or remove ships from the list button (top right) by name
  or MMSI.
- **Home Assistant entities.** Each ship appears as
  `device_tracker.ferry_watch_<name>`, updated about once a minute, so the
  ships also show on the normal HA map.

Notifications, the area editor and stats come in the next versions.

## Options

| Option | Default | What it does |
| --- | --- | --- |
| `retention_days` | 60 | How long ship positions are kept for tracks. |
| `ha_entities` | true | Publish each ship as a `device_tracker`. |
| `log_level` | info | How much the add-on logs. |

## Resource use

The add-on keeps one websocket open and stores about one point every 30
seconds per moving ship (every 5 minutes while in port). Expect well under 1%
CPU, about 50 MB of memory, and a database of a few tens of MB with the
default 60 days.

## If a ship never shows up

If a ship has been re-flagged, its MMSI changes. For the four default ships the
add-on then looks the ship up by its exact name and switches to the new MMSI
automatically (see the log). For other ships, remove the ship and add it again
by name.

## Data sources

- Ship positions: [Digitraffic](https://www.digitraffic.fi/en/marine-traffic/), Fintraffic, CC BY 4.0.
- Maps: © OpenStreetMap contributors, © CARTO; imagery © Esri; sea marks © OpenSeaMap;
  nautical chart © Traficom.
- Ship photos: Wikipedia / Wikimedia Commons, credited on each photo.
