# Changelog

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
