# Changelog

## 0.11.1

- Drawn chart: leaves out railway-station style names such as
  "HANKO, POHJOINEN" that aren't chart names.

## 0.11.0

- New **Official** chart style: Traficom's electronic chart from its S-57 view
  service, with everything the official chart has (rocks, wrecks, names,
  cables, light descriptions), seamless and free (CC BY 4.0).
- Drawn chart: rocks (underwater +, awash, above water), rock clusters and
  wrecks, and island, place and water names, once you paste a free Land
  Survey API key into menu → Ships → Chart. Light descriptions (e.g.
  "Iso WRG 4s") next to lights, and piers and breakwaters.
- Drawn chart symbols closer to Traficom's charts: starboard spar buoys end in
  a point, light flares point south-east, lighthouses are black stars and
  lights black dots.
- Tapping a ship's track shows the speed in knots only.

## 0.10.0

- **Keep chart on the NAS** (menu → Ships → Chart, on by default): downloads the
  drawn chart of the whole Gulf of Finland once in the background, one area at a
  time (385 areas, Hanko to the eastern border), with progress shown in the
  menu. After that every area opens without waiting for a download, and the data
  is refreshed monthly. Switch it off to download only what you look at.

## 0.9.2

- Fixes the panel (and sometimes Home Assistant itself) spinning forever on the
  drawn chart. Chart requests used to wait up to half a minute while an area's
  data downloaded, and a browser only opens a few connections at once, so
  everything else queued behind them. Now they answer at once, the chart fills
  in as each area arrives, and "Downloading chart data…" shows meanwhile.
- Areas are smaller (about 10 km) and download one at a time, data is parsed
  off the web server's thread, each download is capped at 20 MB, areas far
  from Finnish waters aren't downloaded at all, and an area the map has moved
  away from is skipped. The drawn chart starts at zoom 10 (the overview chart
  covers wider views).

## 0.9.1

- The drawn chart keeps at most 6 areas' data in memory (was 12), to stay light
  on RAM; others are read back from the NAS's disk when needed.

## 0.9.0

- New drawn chart, like the Luotsi app: the add-on draws the chart itself from
  Traficom's depth data and Väylävirasto's fairway data, so it looks the same
  everywhere, with no scanned sheet edges or inset boxes. Land is pale yellow,
  shallow water in three blues (0–3, 3–6, 6–10 m), deeper water white, with
  depth contours, soundings, fairway lines with their draught, fairway areas,
  leading lines, buoys and beacons in their IALA colours with topmarks, lights
  with their magenta flare, and coloured light sectors.
- Tap a buoy, beacon or light on the drawn chart to see its name and type.
- **Drawn / Scanned** under the map buttons switches between the new chart and
  Traficom's scanned chart.

## 0.8.0

- Tap a ship and its voyage track is coloured by speed, from purple (stopped)
  through blue, green and amber to red (25 kn and over), with a scale and the
  top speed on the card. Tap the track to see the speed and time at that spot.
- Rescue aircraft popups now show a photo of the aircraft (from
  Planespotters.net, credited and linked), its type in plain words, operator,
  owner, year built, altitude, speed in knots and km/h, whether it's climbing
  or descending, any emergency, and a link to follow it on adsb.fi.

## 0.7.1

- Chart view no longer shows two sets of buoys in Finnish waters: OpenSeaMap's
  sea marks now appear only beyond them, where Traficom's chart doesn't reach.
- Beyond Finnish waters the chart view shows Esri's ocean map with depth
  shading, toned to sit next to Traficom's chart. Sweden and Estonia don't
  publish free charts.

## 0.7.0

- Stats can be grouped by years, months, weeks, weekdays or hours, with the
  busiest one named under the chart. Tap a bar to see just that period.
- Tap a month to get a calendar of it: each day shows a dot per inner-route
  passage in the ship's colour; tap a day to see that day's passages.

## 0.6.6

- Alerts switch per ship in the Ships tab. The four ferries alert; ships you
  add later are tracked and counted but don't alert unless you switch it on.
  Fleet, followed and rescue ships never alert.

## 0.6.5

- Chart showed only chart series A (east of Helsinki). It now stacks
  Traficom's overview charts with all chart series, so the whole coast is
  covered. The chart tiles are fetched again once after this update.
- A badge under the top bar shows when rescue aircraft are in the air ("FinnHEMS
  10 in the air", "2 helicopters in the air"); tap it to zoom to them.

## 0.6.4

- Faster chart: nautical chart and sea mark tiles now come through the add-on,
  which keeps a copy on the NAS (up to 300 MB, refreshed monthly). The first
  view of an area is as slow as before; after that it loads quickly on every
  device.

## 0.6.3

- Ships moored in port no longer show as "Last seen 4 h ago": moored ships
  often send few or no new AIS positions, so a ship stopped at a known port
  shows "In port in Helsinki since 10:31" and only counts as out of contact
  after a day.

## 0.6.2

- Chart: the nautical chart flashed and was then covered by the normal map; the
  chart now stays on top of it. Place names in the satellite view now show
  above the imagery too.

## 0.6.1

- After an update, phones could keep running the old panel from their cache
  (for example still showing "API key required" on the map). The panel's
  files now carry the version number, so an update always loads the new ones.

## 0.6.0

- Rescue and coast guard fleet: Finnish ships that report themselves as search
  and rescue, Border Guard (law enforcement) or military in AIS. Navy ships
  mostly keep AIS off, so few of them appear.
- Icebreakers fleet: Urho, Sisu, Otso, Kontio, Voima, Polaris, Fennica, Nordica.
- Rescue aircraft: FinnHEMS, Border Guard and police helicopters and Border
  Guard planes from adsb.fi, with a 10-minute trail. Fetched every 10 seconds
  only while the switch is on and the map is open; nothing is stored.
- These three start switched off; turn them on in the Fleets tab.

## 0.5.0

- Map and Chart showed "API key required": CARTO's map tiles now need a key, so
  the map uses Esri's free tiles (the same service as the satellite view). The
  alert picture uses them too.
- Finferries fleet, including the Utö route ferries (Utö, Kökar, Tuuli).
- Follow any ship: search under Add a ship and choose Follow to show it on the
  map with its latest trip, without alerts or stats.
- Fleet and followed ships get a photo from Wikipedia (English, Finnish or
  Swedish) when there is one; you can also set your own.
- Tapping one of your ships shows its inner-route numbers: this month, this
  year, the inner-route share, the last inner passage and the usual times.
- The menu shows which chart layer is in use.

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
