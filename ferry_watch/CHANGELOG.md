# Changelog

## 0.4.2

- Tapping an alert opened a "404: Not Found" page; it now opens the Ferry Watch panel.

## 0.4.1

- Area editor: draw an inner-route line at each end of the inner route; the
  outer-route line is gone (no line crossed means the outer route).

## 0.4.0

- Fleets tab in the menu: Tallink Silja, Viking Line and Finnlines ships on the
  map as smaller markers with their current voyage, each fleet with its own
  on/off switch. Tap a fleet ship for its card and track, or add it to your
  ships to get alerts and stats for it too.
- Fleet membership is worked out daily from Digitraffic's vessel list by name,
  so new ships appear by themselves.
- Fleet ships are stored more sparsely (every minute while moving) and only
  for 3 days, which is enough for their current voyage.

## 0.3.0

- Stats tab in the menu: inner-route passages per month as a chart, and per
  ship the count towards Helsinki and towards Stockholm, the usual time of
  day, and the inner-route share when outer-route lines are drawn. Tap a month
  to see it on its own; pick another year once there is one.
- Recent passages moved to the Stats tab; tap one to replay its track on the
  map (an hour either side, with a dot every 10 minutes).
- New `sensor.ferry_watch_last_inner_passage` and a `ferry_watch_passage`
  event for every passage, for your own automations.
- The "no ship data" warning is only sent between 06:00 and 21:00.
- Download all passages as a CSV file.
- Passages of a removed ship stay in the stats, and move with a ship whose
  MMSI changes.

## 0.2.0

- Area editor: draw inner-route lines, outer-route lines, your own areas and
  inner-route areas on the map, and drag their points to adjust them. Your old
  inner-route box and coastal area are copied in on first start.
- Inner-route alerts to your phone, with a map picture of the ship and its
  track, the direction (towards Helsinki or towards Stockholm) and when it
  passes nearest you. Tapping the alert opens the panel on that ship.
- Alerts only while your phone is in your area; Ferry Watch asks the phone for
  a fresh location when the last one is old.
- Warning when ship data stops for 30 minutes.
- Recent passages list and a test notification button in the ships list.

## 0.1.1

- Street map now uses CARTO tiles; OpenStreetMap's own servers block embedded panels ("Access blocked").
- Ship destinations sent as port codes are shown as names, e.g. "route Stockholm – Mariehamn – Helsinki".

## 0.1.0

- First version: live map of the four inner-route ferries, tracks since last
  port, map/satellite/chart layers, ship photos, ship list, and
  `device_tracker` entities in Home Assistant.
