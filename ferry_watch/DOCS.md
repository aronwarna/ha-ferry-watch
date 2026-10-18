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

- **Inner-route alerts.** When one of the ships crosses an inner-route line
  while your phone is inside one of your areas, your phone gets a
  notification with a small map of the ship and its track. Tapping it opens
  the panel on that ship. Outer-route passages are only logged.
- **Feed warning.** If no ship positions arrive for 30 minutes, you get one
  notification, and another when they're back.

- **Stats.** The **Stats** tab in the menu (top right) counts inner-route
  passages per month, per ship and per direction (towards Helsinki or towards
  Stockholm), with each ship's usual time of day. If you also draw
  outer-route lines, it shows how often each ship chooses the inner route.
  **Download all passages (CSV)** gives you the raw list.

## Areas

Open the area editor with the shape button at the top right. The first start
copies two shapes from the old YAML setup: the inner-route box and your
coastal area. You can keep, change or delete them.

- **Inner route line** (recommended). Draw a short line across the inner
  fairway where the outer route doesn't go. A ship counts as taking the inner
  route the moment its track crosses the line, so it fires once, not for the
  whole hour the ship spends in the archipelago. Two or three lines along the
  route make a missed position harmless; the alert is sent once per ship per
  two hours.
- **Outer route line.** Optional. Lets the passage list and stats record
  outer-route trips too. These never notify.
- **My area.** Draw the area where you want alerts, e.g. the coast near home.
  If you have none, alerts go out wherever your phone is.
- **Inner route area.** The old style: a ship counts once it has two positions
  in a row inside the area.

Drag the points of any shape to adjust it. Changes are saved straight away.
A ship only counts when it is moving at 5 kn or more, and a gap of more than 20
minutes between two positions is not treated as a crossing.

## Notifications

The **Send a test notification** button in the menu's Ships tab (top right) sends a
sample alert for the ship that moved most recently, and tells you whether a
real alert would go out where your phone is now.

Before an alert, Ferry Watch reads your phone's location from Home Assistant.
If it is older than `phone_max_age_minutes`, it asks the companion app for a
fresh one and waits up to a minute. Alerts about positions more than 10
minutes old (for example after a restart) are logged but not sent.

The notification picture is saved in Home Assistant's media folder
(`/media/ferry_watch`) and removed after 14 days.

## Options

| Option | Default | What it does |
| --- | --- | --- |
| `retention_days` | 60 | How long ship positions are kept for tracks. |
| `ha_entities` | true | Publish each ship as a `device_tracker`. |
| `notify_service` | `notify.mobile_app_samsung_galaxy_s22` | The companion app service that receives alerts. |
| `phone_entity` | `device_tracker.samsung_galaxy_s22` | The phone's location, for the area check. |
| `phone_max_age_minutes` | 15 | How old the phone's location may be before Ferry Watch asks for a new one. |
| `require_phone_area` | true | Only alert while the phone is inside one of your areas. |
| `notify_feed_problems` | true | Warn when ship data stops arriving. |
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
