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
  The chart has three styles, switched under the buttons:
  - **Drawn** (the default) is drawn by the add-on from Finland's open chart
    data, like the Luotsi app: Traficom's depth areas, contours and soundings,
    and Väylävirasto's fairways, buoys, beacons, lights and light sectors, in
    the colours and symbols of Finnish paper charts. It is seamless, with no
    sheet edges. Tap a buoy, beacon or light to see what it is. Zoomed far
    out it shows Traficom's overview chart. The first time you look at an
    area the add-on downloads its data, so it takes a moment to appear;
    **Keep chart on the NAS** (menu → Settings → Chart storage) downloads the whole Gulf
    of Finland ahead. With a free Land Survey (Maanmittauslaitos) API key,
    pasted in the same place, it also shows rocks, rock clusters, wrecks and
    place names.
    Outside Finnish waters (the Estonian coast, the Åland Sea and the Swedish
    coast from south of Stockholm up the Gulf of Bothnia) it is drawn in the
    same style from open European data, so there is no visible edge where
    Finland's data ends: depths and contours from the **EMODnet Bathymetry**
    depth model (about 115 m apart, CC BY 4.0), and the coastline, buoys,
    beacons, lights with their characters and sectors, rocks, wrecks, traffic
    separation schemes, leading lines and names from **OpenStreetMap**
    (OpenSeaMap's sea mark tagging, © OpenStreetMap contributors, ODbL, read
    through the Overpass API). There are no soundings there, and the depths
    are a smoothed model, so shoals can be shallower than shown. Sweden's and
    Estonia's own charts aren't open data, so they aren't used.
  - **Official** is Traficom's electronic chart (the S-57 data official ENCs
    are made from), drawn by Traficom's own S-57 view service: everything on
    the official chart, seamless.
  - **Scanned** is Traficom's scanned paper chart sheets.

  Beyond Finnish waters (Sweden and Estonia have no free charts) all show
  Esri's ocean map with OpenSeaMap sea marks. Not for navigation.
- **Ship photos.** Each ship's photo comes from its Wikipedia article. Use
  **Change photo** on a ship's card to upload your own.
- **More ships.** Add or remove ships in the menu (top right) under **Ships**,
  by name or MMSI.
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
  Stockholm), with each ship's usual time of day. Outer-route sailings are
  counted too, so the stats can show how often each ship takes the inner
  route: a ship that crosses the Porkkala meridian (24.40° E, between
  59.55° and 60.15° N) without crossing one of your inner-route lines within
  two hours counts as an outer-route passage. These only feed the stats; they
  never alert and don't count towards the next-passage guess. (Outer-route
  lines from earlier versions still work, and then the meridian isn't used.)
  **Download all passages (CSV)** gives you the raw list. Tap a passage to
  replay its track on the map, and ▶ to play it back (see below).
- **Passage weather.** Each passage keeps the weather when the ship passed,
  whether or not the panel is open: wind, gusts and direction at Mäkiluoto,
  the sea temperature, the visibility at Harmaja and, from December to April,
  the ice on the inner route. Stats shows it on each passage (an arrow
  pointing where the wind blows, m/s with gusts in brackets, the sea
  temperature, and "Ice 28 cm · 90%" or "No ice"). Tap a day in the month
  calendar to list that day's passages with their weather. The **Weather**
  section compares the inner and outer route by wind strength (0–5, 5–10,
  10–15 and 15+ m/s), by wind direction (tap a direction for its numbers) and,
  in winter, by ice, for whatever the chart above shows (a year, a month or a
  day). See *Sea ice* below for where the ice comes from.

## The ship card and track playback

Tap a ship to open its card. On a phone the card is a bottom sheet: drag it
down to leave a slim banner (photo, name and what the ship is doing) so the
map is free, drag or tap the banner to open the card again, and flick it down
from the banner to close it. On a computer the card stays at the side; its ⌄
button makes it small the same way. **Show whole track** makes the card small
on a phone and fits the ship's track above it.

**Play back** (or ▶ on the banner) sails the ship along its stored track again,
as on MarineTraffic: the track is drawn behind it in speed colours, and the
player has play and pause, a time slider with the time and speed at that point,
and a speed button (×60, ×300, ×1200 or ×3600 real time; it starts at the one
that plays the track in about a minute). The map follows the ship. ✕ on the
player or Escape stops it. A passage opened from Stats has the same player
(▶ next to the passage), for the two hours around the passage.

## The menu

The menu button (top right) has four tabs:

- **Ships**: your ships, with their alert switches, and **Add a ship**.
- **On the map**: switches for what the map shows besides your ships (sea
  conditions, other ships, rescue aircraft, weather stations, weather layers,
  harbours, faulty lights and buoys, wrecks and coastal heritage), your saved
  routes and the fleets.
- **Stats**: inner-route passages, see above.
- **Settings**: notifications and the test button, chart storage, the Land
  Survey API key, data sources, the version and the memory the add-on uses,
  with where it goes (chart, foreign depths, database, ships, aircraft, and the
  program itself: Python and its libraries).

The switches for other ships, weather stations, weather layers, harbours,
faulty lights and heritage are remembered by each browser or phone on its own;
the others are kept by the add-on.

## Next passage

Ferry Watch predicts when each of your ships will reach the Porkkala
inner-route lines, from how long its earlier sailings took from about the same
place, adjusted for its speed now. The ship's chip says **Porkkala in 23 min**,
and its card **Reaches the Porkkala lines ≈18:42 (in 23 min) · inner route 9 of
last 14 sailings**: how often it took the inner route on its last sailings that
way in the past four weeks. It needs a few recorded sailings in each direction
before it says anything, and it is a guess: ships wait for a berth, slow down in
ice or change their schedule.

The card also shows the ETA the crew entered in AIS (**ETA Stockholm 09:45
(AIS)**) and the draught they report.

## Sea conditions and the sun

A pill under the top bar shows the wind (an arrow pointing where it blows), the
sea temperature and the next sunrise or sunset. Tap it for the details: wind
and gusts at Mäkiluoto, air temperature, visibility and weather at Harmaja, sea
temperature and sea level at Kaivopuisto, the wave height from the Gulf of
Finland buoy, each with its station and observation time, and sunrise, sunset
and dark. On a phone the dot at its left is the ship feed (green live, red
offline, purple simulated).

The observations come from the Finnish Meteorological Institute's open data and
are read every 10 minutes, only while the panel is open, and for an alert
(which waits at most 3 seconds for them). The alert then ends with a line like
"Wind SW 9 m/s, visibility 20 km, sunset 18:12." and the wind is saved with the
passage (it's in the CSV). Every other passage, alert or not, also fetches
them once (if they're more than 15 minutes old) so that the passage keeps its
weather. Sunrise and sunset are worked out by the add-on for your areas.
Switch the pill off under **On the map → Sea conditions**.

## Sea ice

From December to April the add-on checks the ice on the inner route once a day
(and again an hour later if a source didn't answer), stores it for the day and
with each passage that day. Two free sources, neither needing a key:

- **FMI's manual sea ice observations** at Kirkkonummi Porkkala (station
  103933, 59.95° N 24.33° E), the WFS stored query
  `fmi::observations::seaice::manual::timevaluepair`: ice thickness and snow on
  the ice, measured by hand about once a week. A report from the last 12 days
  counts; no report in a month means no ice there.
- **The FMI ice chart via the Copernicus Marine Service** (Baltic sea ice
  concentration, `SEAICE_BAL_SEAICE_L4_NRT_OBSERVATIONS_011_004`), read for
  the point 59.97° N 24.40° E through its public map service.

The chart decides "ice" (10% cover or more) or "open water" for the route; the
thickness comes from the Porkkala observations. If a source can't be read, the
log says so once until it works again, and the day is stored with what the
other one gave. Outside December to April nothing is fetched.

## You are here

While the panel is open, Ferry Watch reads your phone's location
(`phone_entity`) from Home Assistant once a minute and shows it as a blue dot,
with a pale circle for its accuracy. A ship's card then says how far it is from
you and, if it is under way, how close it will come on its present course and
when ("4.2 km from you · closest 1.1 km at 18:42"). Locations older than
`phone_max_age_minutes` aren't shown.

## Other ships

**On the map → Other ships** (off by default) shows all AIS traffic around the
part of the map you're looking at as small triangles pointing along the course,
coloured by type: passenger blue, cargo green, tanker red, high speed yellow,
tugs, pilots and service boats teal, fishing orange, sailing and pleasure
magenta, anything else grey. Tap one for its name, type, length, speed and
destination, and **Follow** to keep it on the map like a followed ship.

They are asked for every 30 seconds and when you move the map, only while the
switch is on and the panel is visible, for a circle of at most 60 km. Nothing is
stored: the add-on keeps each answer for 20 seconds and names the ships from
its daily vessel list.

## Faulty lights and buoys

On the chart view, navigation aids reported faulty (unlit, damaged, missing or
off position) show as a red ring with "!". Tap one for "Unlit since 3 Oct", the
light or buoy and its fairway. The list comes from Digitraffic and is read at
most every 30 minutes, only while the chart view is shown. Switch it off under
**On the map → Faulty lights and buoys**.

## Wrecks and coastal heritage

**On the map → Wrecks and coastal heritage** (on by default) shows wrecks and
coastal defence sites (bunkers, trenches, gun and firing positions, near the
shore and on islands, from zoom 13) from Museovirasto's registers on the drawn
chart. Tap one to see what it is.

## Measure and plan a route

The ruler button (top, next to the areas button) turns on route mode. Tap the
map to add points; each leg shows its distance in nautical miles and its true
bearing, and the panel shows the total and the time and ETA at the speed you
enter (25 kn to start with). Drag a point to move it, tap it to remove it;
**Undo** (or Ctrl+Z) and the bin button take back or clear. Escape or the ✕
leaves route mode; the route you were drawing is kept in the browser until you
come back. On a phone the panel is a small sheet above the ship chips, on a
computer a list on the left.

**Auto** plans a route along the marked fairways: enter your draught (1.5 m to
start with, remembered), tap the start and the end (or use the first and last
point) and press **Plan along fairways**. The add-on follows the fairway centre
lines of the drawn chart, skips fairways whose design depth is less than your
draught plus 0.3 m, and fills the panel with a route you can edit. It plans up
to about 50 NM at a time and uses the chart areas along the way, downloading
the missing ones first (it says so when they're still on their way). It is for
planning only, not for navigation: check it on the chart.

Give a route a name and **Save** it to keep it in the add-on. Saved routes show
as dashed lines with their name; tap one for its distance and **Edit route**.
**On the map → Routes** lists them with a switch to show or hide each, **Edit**
and **Delete**. There is no navigation guidance.

## Weather stations

**On the map → Weather stations** (on by default, every map view) shows the
Finnish Meteorological Institute's coastal and sea stations as discs with the
wind in m/s, coloured by strength, and a pointer showing where the wind blows.
Wave buoys and sea level stations show waves or sea temperature. Tap a station
for its name, how long ago it reported, wind, gusts, direction, air temperature,
sea temperature or wave height where measured, and the wind and gusts over the
past 24 hours. The add-on asks FMI at most every 10 minutes, and only while the
panel is open; the 24 hours are fetched when you open a station.

## Weather layers and the time bar

Weather layers such as the sea ice chart, the rain radar and the wind forecast
(added one by one in later versions) get a switch each under **On the map**,
with an opacity slider once switched on. While a layer with more than one time
is on, a time bar sits above the ship chips: play, pause, step a frame back or
forward, or drag to a time. It starts at the newest frame and keeps showing the
newest as new ones come in. With several layers on, the bar steps through all
their times together and each layer shows its own frame for that moment (or
nothing, if it has none then). The bar gives way while a ship card, a replay
or the area editor is open. Which layers are on and how see-through they are is
remembered by each browser or phone on its own. A layer whose source can't be
reached says so under its switch and in Settings, and tries again a few
minutes later.

## Harbours

**On the map → Harbours** (on by default) shows marinas and harbours from
OpenStreetMap from zoom 11: a red sailboat on the chart view, a blue tag on the
map and satellite views. Tap one for its services (guest berths, fuel, toilets,
shower, sauna, electricity, water, waste, slipway, restaurant), depth, website
and phone, as mapped in OpenStreetMap. The list is downloaded the first time
it's needed and refreshed once a month; missing services may just not be mapped
yet.

## Fleets

**On the map** in the menu shows the Tallink Silja, Viking Line, Eckerö Line,
Finnlines and Finferries fleets (Finferries includes Utö, Kökar and Tuuli on the
Utö route). Each fleet has a switch: when it's on, its ships appear on the map as
smaller markers with a short tail, and tapping one shows its whole current
voyage and a photo when Wikipedia has one. **Add to my ships** on a fleet ship's
card makes it one of your ships, with alerts and stats.

To show any other ship, search for it under **Add a ship** and choose
**Follow**. Followed ships are listed under **Followed ships** in the Fleets tab
and are kept like fleet ships: only their latest trip is stored.

More switches start off: **Suomenlinna ferries** (Suokki, Suomenlinna II and
Ehrensvärd), **Cruise ships** (passenger ships over 180 m not flagged in
Finland, Sweden or Estonia, seen in the past two weeks), **Rescue and coast guard** (Finnish ships
whose AIS says search and rescue, Border Guard or military; the Navy mostly
keeps AIS off), **Icebreakers**, **Pilot boats and tugs** (Finnish pilot boats
and tugs), and **Rescue aircraft**: FinnHEMS, Border Guard and police
helicopters and Border Guard planes, from [adsb.fi](https://adsb.fi)'s open
ADS-B data. While that switch is on, aircraft are fetched every 10 seconds
while the panel is open and every 30 seconds while it is closed, so that their
flights are recorded (see below). Fleet ships that report every few seconds are
passed to the panel at most once every 5 seconds each.

### Rescue aircraft history

While **Rescue aircraft** is on, the add-on records the flights, panel open or
not. A position is kept when 30 seconds have passed since the last one kept,
or the aircraft turned more than 15° or climbed or descended more than 100 m;
an aircraft standing on the ground isn't recorded. A gap of 20 minutes ends a
flight. Flights are kept for a year and older ones are deleted once a day.

- **Tap an aircraft** for its card: its whole flight so far is drawn as a thick
  line coloured by altitude (amber low, through green and blue, to violet
  above 2 km), with the scale on the card. **History** opens the history on
  that aircraft.
- **Aircraft history** (the button with the line next to the aircraft badge,
  or menu → On the map → Rescue aircraft) dims the map and draws every
  recorded flight of the last 24 hours, 7 days, 30 days or year as glowing
  lines, one colour per aircraft. **Heat** shows instead where they fly most.
  ▶ plays the period as a time-lapse with a clock, each flight drawn as it
  happened with a bright head; drag the slider to move in time (the small bars
  above it show when aircraft were up). The list shows each aircraft (with
  its photo if you've opened its card before), its flights and hours; tap one
  to show only its flights. Tap a line on the map for that flight's date,
  times, duration, highest altitude and distance, drawn in full in altitude
  colours. Escape backs out a step at a time.

Ships are matched by name (and for some fleets type, length or flag) once a day
from Digitraffic's vessel list, so new
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

Only ships with **Alerts** switched on in the Ships tab send alerts: the four
ferries by default. Ships you add later are tracked and counted in the stats
but stay quiet until you switch it on. Fleet, followed and rescue ships never
alert. The direction comes from which way the ship crossed the line: east is
"towards Helsinki", west "towards Stockholm".

The **Send a test notification** button in the menu's Settings tab sends a
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
- `sensor.ferry_watch_next_passage`: when the next predicted passage of a ship
  with alerts on reaches the Porkkala lines (a timestamp, `unknown` when none
  is coming), with `ship`, `mmsi`, `direction` and, when known, `inner_route`
  out of `sailings` (its habit on recent sailings that way). It changes only
  when the prediction moves by more than two minutes. For example, a reminder
  ten minutes ahead: a template trigger on
  `{{ now() >= states('sensor.ferry_watch_next_passage') | as_datetime - timedelta(minutes=10) }}`.
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
under 1% CPU, about 60 to 90 MB of memory (most of it Python and its libraries),
and a database of a few tens of MB. Once
a day the add-on downloads Digitraffic's vessel list into a small table in its
database, which the fleets, ship search and the other-ships layer use; it is read
item by item so it never sits in memory whole. Sea conditions, your phone's
location, other ships and faulty lights are only fetched while the panel is open
(and the switch is on), and nothing is polled while it is closed, except as follows. Rescue aircraft add one small request every 10 seconds while the panel is open and, while their switch is on, every 30 seconds while it is closed; their flights take about 6 kB of database per hour flown (a few MB a year), and the memory holds only each airborne aircraft's latest position. The history view's flights are sent to the browser as simplified lines, a few at a time, so even a year is never in memory whole. Each passage fetches the sea conditions once if they're more than 15 minutes old, and from December to April the ice is checked once a day (two small requests). Weather stations add three small requests to FMI at most every 10 minutes while the panel is open, kept in a few kilobytes. The harbour list (well under 1 MB on disk) is read into memory only while harbours are being shown and let go after 10 minutes. Automatic route planning reads only the fairway lines of the chart areas along the way and keeps the result for a few minutes only if it is small. Chart and sea mark tiles you have viewed are kept in a cache of up to 300 MB, and drawn chart tiles in one of up to 64 MB. Weather layers are only fetched for the layers switched on while the panel is open; their tiles are kept in a cache of their own of up to 80 MB, each layer's frames for as long as that layer says (a radar frame for hours), and removed from the disk after that. While the panel is hidden for a minute (phone locked, another app), its live connection closes and the panel-only fetching stops until it is seen again. The drawn chart's data is downloaded an area at a time (about 20 km across) when first viewed, refreshed after a month, and removed when not viewed for two months; the menu shows how much is stored. Outside Finnish waters an area takes one or two requests to the Overpass API (streamed and drawn into a small depth grid as it arrives, so even the Stockholm archipelago's coastline is never held in memory whole) and EMODnet's depth model is fetched for about 40 km squares (under 1 MB each, kept on disk for half a year, a few hundred kB compressed).

## If a ship never shows up

If a ship has been re-flagged, its MMSI changes. For the four default ships the
add-on then looks the ship up by its exact name and switches to the new MMSI
automatically (see the log). For other ships, remove the ship and add it again
by name.

## Data sources

- Ship positions: [Digitraffic](https://www.digitraffic.fi/en/marine-traffic/), Fintraffic, CC BY 4.0.
- Faulty lights and buoys: Digitraffic (Fintraffic / Väylävirasto), CC BY 4.0.
- Sea conditions, weather stations and sea ice observations: [Finnish Meteorological Institute](https://en.ilmatieteenlaitos.fi/open-data) open data, CC BY 4.0.
- Ice chart: E.U. Copernicus Marine Service Information (FMI's Baltic sea ice concentration).
- Harbours: © [OpenStreetMap contributors](https://www.openstreetmap.org/copyright), ODbL, via the Overpass API.
- Wrecks and coastal heritage: Museovirasto (Finnish Heritage Agency) registers.
- Drawn chart outside Finnish waters: depths from the [EMODnet Bathymetry](https://emodnet.ec.europa.eu/en/bathymetry)
  digital terrain model (EMODnet Bathymetry Consortium, CC BY 4.0); coastline, sea marks, lights, routeing
  and names © [OpenStreetMap contributors](https://www.openstreetmap.org/copyright), ODbL, with
  [OpenSeaMap](https://www.openseamap.org)'s sea mark tagging, via the Overpass API.
- Aircraft: [adsb.fi](https://adsb.fi) open data (personal, non-commercial use).
- Maps, ocean map and imagery © Esri, GEBCO, NOAA, HERE, Garmin, © OpenStreetMap contributors; sea marks © OpenSeaMap;
  nautical chart and depth data © Traficom (Traficom hydrographic data
  licence, not for navigation, does not meet the requirements of an official
  nautical chart); fairway and navigation aid data © Väylävirasto (CC BY 4.0); aircraft photos © their photographers via
  Planespotters.net (loaded by your browser, never stored by the add-on).
- Ship photos: Wikipedia / Wikimedia Commons, credited on each photo.
