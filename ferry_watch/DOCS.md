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
  notification (only between 06:00 and 21:00), and another when they're back.

- **Stats.** The **Stats** tab in the menu (top right) counts inner-route
  passages per month, per ship and per direction (towards Helsinki or towards
  Stockholm), with each ship's usual time of day. If you also draw
  (Outer-route lines from earlier versions still count for the inner-route share.)
  **Download all passages (CSV)** gives you the raw list. Tap a passage to
  replay its track on the map.

## Fleets

The **Fleets** tab in the menu shows the Tallink Silja, Viking Line and
Finnlines fleets. Each fleet has a switch: when it's on, its ships appear on the
map as smaller markers with a short tail, and tapping one shows its whole
current voyage. **Add to my ships** on a fleet ship's card makes it one of your
ships, with alerts, stats and a photo.

Ships are matched by name once a day from Digitraffic's vessel list, so new
ships join by themselves; a sold ship drops out once it's renamed. Ships that
haven't reported in Finnish waters for a week are left off the map. Fleet ships
never trigger alerts or count in the stats unless you add them to your ships.
Switching a fleet off stops following it entirely.

## Areas

Open the area editor with the shape button at the top right. The first start
copies two shapes from the old YAML setup: the inner-route box and your
coastal area. You can keep, change or delete them.

- **Inner route line.** Draw one short line across the inner fairway at each
  end of the inner route, where it leaves the outer route. Whichever line a
  ship crosses first sends the alert, so it works in both directions; the
  second line is ignored for the next two hours. Ships that don't cross a line
  took the outer route.
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

## For your own automations

- `sensor.ferry_watch_last_inner_passage`: the time of the latest inner-route
  passage, with the ship, direction, speed and whether an alert was sent.
- Event `ferry_watch_passage`: fired for every passage, inner or outer, with
  `ship`, `mmsi`, `route`, `direction`, `time`, `speed_kn`, `latitude`,
  `longitude`, `alert_sent` and `alert_note`.

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

The add-on keeps one websocket open. Your ships are stored about every 30
seconds while moving (every 5 minutes in port) and kept for `retention_days`;
fleet ships every minute (10 minutes in port) and kept for 3 days. Expect well
under 1% CPU, about 50 MB of memory, and a database of a few tens of MB. Once
a day the add-on downloads Digitraffic's vessel list to update the fleets.

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
