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
    sheet edges. Tap a buoy, beacon or light to see what it is. It is drawn at
    every zoom, all of Finland at once included: zoomed out it shows fewer and
    smoother depth contours, only the major lights and the seas' names, as
    small-scale charts do (at zooms 7 and 8 only the lighthouses; at zooms 10
    and 11 lit buoys and beacons that crowd each other are thinned, while a lone
    one out at sea stays; from zoom 13 every contour and mark shows), and where
    the chart has no data yet (or none at all,
    inland) the land, islands and lakes come from Natural Earth's world map, so
    there are no gaps or squares. The first time you look at an area the add-on
    downloads its data, so it takes a moment to appear; **Keep chart on the
    NAS** (menu → Settings → Chart storage) downloads all Finnish waters ahead:
    the Gulf of Finland first, then the Archipelago Sea and Åland, the Gulf of
    Bothnia and the charted lakes, first what the zoomed-out chart needs
    everywhere and then the detail. With a free Land Survey (Maanmittauslaitos) API key,
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
    Out at sea Traficom's depths stop short in places, and in places a shoal
    comes without its own depth area; there the chart shows plain deep water
    (white) with no depth figures. The soundings still show the depth there.
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

## Inner route outlook

At the top of Stats: each ferry sailing expected in the next 48 hours, with its chance of
taking the inner route, the forecast wind, gusts and waves at that time, and what pushes
the chance up or down. It's an experiment. A small model learns from your passages and
the weather at each one (wind strength and direction, gusts, waves, ice, the ships'
recent choices), and it is tested on your own history day by day, using only what was
known the midnight before, against the plain guess of each ship's usual share. "How good
is it?" shows the result. If it does worse than the usual share, the usual share is shown
instead. Weather for past and future comes from Open-Meteo (CC BY 4.0).

### The outlook checks itself

Every chance the outlook works out for a coming sailing is written to a log
before the ship sails, and marked inner or outer once the ship has passed.
**Self-check: its real forecasts so far** under the outlook shows the result:
how many forecasts have been checked, how many inner passages it called (said
50 % or more), the false alarms, how it compares with simply going by each
ship's usual share, how often "50 to 75 %" really went inner, and the same
month by month and by ship. It also scores the forecasts it made half a day or
more ahead on their own.

It adapts in three ways. Passages from the last months count the most when it
learns (one from 120 days ago counts half), so it follows the ships' habits as
they change. Once 40 forecasts have been checked, its percentages are corrected
by how they have matched what happened (pulled towards the middle if it was too
sure, raised or lowered if it ran low or high). And from then on this scorecard,
not the replay of past passages, decides whether the outlook's own chances are
shown or each ship's usual share instead. It keeps logging either way, so it can
earn its place back. The log needs a few weeks before it says anything.

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

## Terrain map

**Terrain** next to Map, Satellite and Chart shows the Finnish Land Survey's
topographic map (maastokartta): contours, paths, buildings, cliffs, marshes and
place names, good for looking at islands and landing places. With your Land
Survey key stored (the same one as for the aerial photos) it comes straight
from the Land Survey; without one it comes from the Kapsi mirror of the same
map. It covers Finland only.

## Faulty lights and buoys

On the chart view, navigation aids reported faulty (unlit, damaged, missing or
off position) show as a red ring with "!". Tap one for "Unlit since 3 Oct", the
light or buoy and its fairway. The list comes from Digitraffic and is read at
most every 30 minutes, only while the chart view is shown. Switch it off under
**On the map → Faulty lights and buoys**.

## Warnings and waterways

**On the map → Warnings and waterways** has three switches. They work on every
map type.

- **Nautical warnings**: the navigational warnings for Finnish waters (from Traficom),
  such as firing exercises, works in a fairway, marks missing or moved and
  things adrift. Each shows as an amber diamond with "!", with its line or area
  dashed in the same colour. Tap the diamond for the text, the place and how
  long it holds. Read every 10 minutes. A single faulty light or buoy is not a
  nautical warning; those are under **Faulty lights and buoys**.
- **Sea weather warnings**: the Finnish Meteorological Institute's warnings for
  the sea areas (wind, waves, icing). The warned areas are tinted yellow,
  orange or red, with a warning sign in the part of each that is in view; tap
  the sign for the text and the hours. Off to start with, because the tint
  covers large areas. A warning that covers your own waters (your phone's
  place, otherwise Porkkala) always shows as a small warning sign with the wind
  speed in the conditions pill at the top, and in full when you tap the pill.
- **Bridges, locks and canals**: Väylävirasto's opening bridges, locks and lock
  canals, from zoom 9. Tap one for its clearance, width, length and depth where
  the data has them, and **All details** for the rest. Opening bridges show
  their clearance next to them from zoom 12. Fetched once every two weeks. The
  open data has no list of fixed bridges; those are on the chart itself.

These are for planning. Check the official sources before you go.

## Wrecks and coastal heritage

**On the map → Wrecks and coastal heritage** (on by default) shows wrecks and
coastal defence sites (bunkers, trenches, gun and firing positions, near the
shore and on islands, from zoom 13) from Museovirasto's registers on the drawn
chart. Tap one to see what it is.

## Measure and plan a route

The ruler button (top, next to the menu button) turns on route mode. Tap the
map to add points, or find a harbour by name with the magnifier button; each
leg shows its distance in nautical miles and its true bearing, and the panel
shows the total and the time and ETA at the speed you enter (25 kn to start
with). Drag a point to move it, tap it for a menu (remove it, or cut straight from it); **Undo** (or Ctrl+Z) and
the bin button take back or clear. Escape or the ✕ leaves route mode; the route
you were drawing is kept in the browser until you come back. On a phone the
panel is a small sheet above the ship chips, on a computer a list on the left.

**Straight / Fairways / Automatic** at the top of the panel says how new legs go
(Automatic is described below). With **Fairways**, enter your draught (1.5 m to start with, remembered)
and tap the start and the end: the add-on plans the leg along the fairway
centre lines of the drawn chart, skipping fairways whose design depth is less
than your draught plus 0.3 m. A fairway leg has a white line down its middle and
a small dot at each turn. Every further tap adds another leg the same way, so
you can mix straight legs and fairway legs.

To change the way the route goes:

- **Drag a dot on the line.** The filled dots are the fairway's turns (and bends
  you've made); the hollow dot halfway along each piece pulls the line out.
  While you drag, the fairways deep enough for your draught glow faintly (from
  zoom 11), and the dot snaps onto one when it comes near, with a ring:
  - **Let go on another fairway** and the route is planned again through that
    point, along that fairway. The point stays as a small diamond (a via point);
    drag it along to change the way again, or tap it to remove it.
  - **Let go anywhere else** and it's a bend: straight lines from the turns
    either side to it, and nothing else moves. Drag a via point off the
    fairways and it becomes a bend the same way. On a computer, hold Shift (or
    Alt) while dragging for a bend right next to a fairway, without snapping.
- **Cut a corner:** tap a stop, a dot or the line, choose **Straight from here
  to…**, then tap where to rejoin the route. Everything between becomes one
  straight line, so a loop round an island is gone; drag its hollow dot to bend
  it through a sound.
- **Tap a leg** (on the map, or in the panel's list) for **Along the fairways**
  (plan that leg along the fairways again; **Fairways, the shortest way** when it
  has via points, which drops them), **Straight line**, or **Add a point here**.
- Moving one of your numbered stops plans the fairway legs next to it again,
  unless you've bent them by hand. Changing the draught plans all fairway legs
  again.

Automatic planning works up to about 50 NM per leg and uses the chart areas
along the way, downloading the missing ones first (it says so when they're
still on their way). It is for planning only, not for navigation: check it on
the chart.

Give a route a name and **Save** it to keep it in the add-on; its fairway and
straight legs are kept, so **Edit** opens it as you drew it. Saved routes show
as dashed lines with their name; tap one for its distance and **Edit route**.
**On the map → Routes** lists them with a switch to show or hide each, **Edit**
and **Delete**. There is no navigation guidance.

### Boats

The boat list at the top of the route panel has four boats. Picking one sets the
draught and the speed (both can be changed by hand afterwards), the depth the
Automatic mode keeps under it, and the weather limits for Boating weather.

| | Size | Draught | Depth it is routed over | Cruising speed |
|---|---|---|---|---|
| Buster M | 4.86 × 1.85 m, 30–40 hp | 0.5 m | 1.0 m | 20 kn |
| Buster XL | 6.05 × 2.20 m, 80–115 hp | 0.6 m | 1.1 m | 26 kn |
| Cabin boat 8 m (Ockelbo B25, Viggo C8.1 class) | 8.0 × 2.7 m | 0.8 m | 1.3 m | 25 kn |
| Sailboat 35 ft | 10.7 × 3.5 m | 1.9 m | 2.5 m | 6 kn |

Sizes and engines of the Busters are from buster.fi; their draughts are not
published and are estimates with the engine down. **Boat and planning numbers** at
the bottom of the route panel lists every number the planning uses.

### Automatic: a route over open water

**Automatic** (the third mode beside Straight and Fairways) plans the leg for the
boat picked, on or off the marked fairways. Tap the start and the end; the add-on
works out the quickest way through water deep enough.

- The drawn chart's depth areas between the two ends become a grid of 40 m cells
  (wider on long trips, up to 400 m; one leg can be 150 NM and more, and the first plan of a long leg can take a minute).
- Water is open where the chart's depth range starts at the depth the boat needs or
  deeper. Where the range only includes it (0–3 m for a small boat: the chart says
  no more), the water is used only if nothing better is near, counted three times
  as long, and that stretch is drawn **amber and dashed**: check it yourself. For
  the sailboat the 0–3 m band is closed.
- Rocks, wrecks and soundings shallower than the boat needs close 30 m round them.
  The shore and shoals are kept 30 m off, and the first 100 m counts a quarter longer.
- Marked fairways deep enough for the boat are always open, so a narrow sound with a
  fairway stays passable, and count a little shorter.
- Inside speed limit areas the boat goes at the limit, so a fast boat is sent round
  a slow area when that is quicker.
- The start and the end may be up to 2.5 km from open water (a berth in a shallow
  bay); the route says so when it is more than 300 m.

The leg can be changed like a fairway leg: drag a dot, add stops, or tap the line
for the menu (**Automatic, over open water** is there for any leg, as is **Plan it
again**). Only chart areas already downloaded count; missing ones are fetched and
the leg is planned again. It is a plan from chart data, not navigation: bridges'
heights, cables, nets and anything not on the chart are not known to it.

### Sailing: fastest and comfortable routes

With the **Sailboat**, an Automatic leg is planned from the wind forecast, the way
ocean races are routed. The boat's speed at each angle to the wind comes from a
speed table for a 35 ft cruiser (in Boat and planning numbers). Too close to the
wind it tacks, dead downwind it may gybe (the speed made good counts, less 7 % for
the turns); under 2 m/s of wind, or when beating with less than 150 m of room, the
engine runs at 5.5 kn. The wind is read hour by hour at up to 30 points over the
area, so a wind shift on the way moves the route.

Two routes come back. **Fastest** is the least time. **Comfortable** counts time
as longer when beating (60 %), running dead downwind (15 %), under engine (10 %),
in wind over 9 m/s (12 % per m/s) and in waves over 0.8 m (60 % per metre), so it
gives way to an easier point of sail or calmer water when that costs little. The
one picked is the route; the other is drawn thin, dashed and purple with the time
it costs or saves, and a tap on it takes it. Beating stretches have dashes down
the middle, stretches under engine none. The route depends on when you leave: tap
an hour in Boating weather and it is planned for that hour. Without a forecast the
leg is the way under engine, and says so.

### Speed limit areas

While the route tool is open (and always with **Speed limit areas** on in Layers)
Väylävirasto's speed limit areas are drawn from zoom 10: a red dashed outline,
lightly tinted, with the limit in km/h.

### Speed limits on the route

Once a route has two points, the add-on checks it against Väylävirasto's speed
limit areas. Under the route you get each limit it runs through, in km/h and
knots, for how many nautical miles, and about how many minutes the limits add at
the speed you set. The stretches are marked in amber on the route with the limit
in a red ring. "No speed limits on this route" means none of the published areas
touch it; signs on the water still decide.

### Routes as GPX files

**On the map → Routes** has **GPX** next to each saved route, which downloads it
as a GPX route for a plotter or another app, and **Import GPX** to read routes
or tracks from a file into your saved routes (long tracks are thinned to 500
points).

### Boating weather: when to set off

Once a route has two points, **Boating weather** under it scores each whole hour
of the next two days as a time to leave: **good** (green), **okay** (amber) or
**bad** (red). The add-on follows the route at the speed in the panel and reads
the forecast where the boat would be when it gets there: wind, gusts, waves,
rain, visibility and daylight (after civil dusk counts as okay at best). The
worst place along the way decides the hour. The line next to the title says the
best windows ("good today 10–15, tomorrow 08–12"); tap an hour for the numbers,
which of them make it okay or bad and how far along the route. A darker foot under an hour
means part of the trip is in the dark. Tap the title to fold it away.

The boat picked at the top of the route panel sets the limits (okay from / bad from):

| | Wind m/s | Gusts m/s | Waves m | Rain mm/h | Sight km |
|---|---|---|---|---|---|
| Buster M | 5 / 8 | 9 / 13 | 0.3 / 0.6 | 0.5 / 2.5 | under 4 / 1 |
| Buster XL | 7 / 11 | 11 / 16 | 0.5 / 1.0 | 0.5 / 3 | under 4 / 1 |
| Cabin boat 8 m | 9 / 14 | 14 / 20 | 0.8 / 1.6 | 1 / 5 | under 3 / 0.8 |
| Sailboat 35 ft | 10 / 15 | 15 / 21 | 1.2 / 2.2 | 1 / 5 | under 3 / 0.8 |

The route itself is coloured for the hour you picked: a green, amber or red band
under the line shows what each stretch is like when the boat gets there.

The forecast is asked for at points along the route (every 3 km, at most 30):
FMI's HARMONIE model for the air (Open-Meteo if FMI doesn't answer), Open-Meteo's
marine forecast for the waves, once an hour for a route. If neither answers, the
weather layers' coarser grid stands in. The wave models hardly know the inner
archipelago: where their nearest sea is more than 2 km from the route, the
waves are counted lower (half from 8 km), which is a guess. It's for planning,
not a promise: a forecast for small waters is never exact.

## Weather stations

**On the map → Weather stations** (on by default, every map view) shows the
Finnish Meteorological Institute's coastal and sea stations as discs with the
wind in m/s, coloured by strength, and a pointer showing where the wind blows.
Wave buoys and sea level stations show waves or sea temperature (teal), and
stations that don't measure the wind their air temperature (grey). Tap a station
for its name, how long ago it reported, wind, gusts, direction, air temperature,
sea temperature or wave height where measured, and the wind and gusts over the
past 24 hours. The add-on asks FMI at most every 10 minutes, and only while the
panel is open; the 24 hours are fetched when you open a station.

## Weather layers and the time bar

The weather button opens the weather panel, laid out like Windy. At the top, one
**colour layer** at a time covers the map: Wind, Temperature, Wind gusts, Clouds,
Rain and snow, Fog and visibility, Waves, Sea temperature, Sea level, Northern
lights, Sunset & sunrise and Light pollution. Tap a chip to show it (tap it again to
hide it); the chosen one gets its note, opacity slider and options below the chips,
and a colour legend sits at the bottom of the map. Under **On top** are the layers
that can go over any colour layer: pressure lines (isobars every 2 hPa with the highs
and lows marked), lightning, the rain radar, the sea ice concentration (November to
May) and the **Ice chart**.

The **Ice chart** is the Finnish Ice Service's daily chart (the one FMI publishes as a
PDF), drawn on the map in the chart's own colours: drift ice by how close it is (light
blue under 1/10, green 1-3/10, yellow 4-6/10, orange 7-8/10, red 9-10/10) and grey for
fast or consolidated ice, with the thickness in cm written on the ice from zoom 7. It
is cut to the water of the drawn chart, so it stays off the land. Under its switch
pick any day of this or last winter (or *Newest* / *Last winter*); while the newest
chart has no ice yet, last winter's (late February) is shown, and says so. Tap the ice
for its class, concentration and thickness.

**Tap the map** while a layer is on to read its value there (temperature, gusts,
cloud at each height, the wave height, period and swell, the sky's brightness...).
The popup gives way to the ship and chart marks when you tap one of those.

While a layer with more than one time is on, a dark time bar sits above the ship
chips, with the days marked: play, pause, step a frame back or forward, or drag to a
time. It starts at the newest frame (for forecasts, the coming hour). With several
layers on, the bar steps through all their times together and each layer shows its
own frame for that moment. The bar gives way while a ship card, a replay or the area
editor is open. Which layers are on and how see-through they are is remembered by
each browser or phone on its own. A layer whose source can't be reached says so in
the panel and in Settings, and tries again a few minutes later.

The forecast layers (wind, temperature, gusts, clouds, rain and snow, fog, pressure)
all share one run of FMI's HARMONIE weather model on a grid of about 30 km from
southern Sweden and Latvia to the Bay of Bothnia and Karelia, 48 hours ahead; if FMI can't be
reached, the same from Open-Meteo. The colours blend smoothly between the grid points
and the values are written on the map from zoom 6. Fog shows the model's own
visibility; where it has none, a fog risk from the humidity, the dew point, the wind
and low cloud ("estimated" on the legend). Rain turns to snow colours where it's
below freezing.

**Waves, sea temperature and sea level** come from Open-Meteo's marine forecast
(48 hours) at the same sea points as the wind: the wave height with moving swell
lines in the direction the waves travel (switch them off under the chip), the sea
surface temperature, and the sea level against its mean in cm. They are drawn only
over the sea, on every map: cut to the drawn chart's water (Traficom's depth areas,
the Swedish and Estonian side's, Natural Earth's open sea, less the lakes), which
the add-on sends for the view at a zoom coarser than the map's (a few kB a tile,
the last few kept made). The colours reach every bay and harbour; the swell lines
and the numbers keep to the water too, and a tap on the land gives no value.

**Lightning** shows FMI's strikes from the last hour, white when new and fading to
red, read every 5 minutes. **Northern lights** shows NOAA's aurora nowcast (the chance
of aurora overhead in the next half hour) and, under it, the chance of seeing it here
with the darkness and the clouds; the panel also gives tonight's Kp forecast in words.

**Light pollution** is the Light Pollution Atlas 2025 (D. Lorenz, from VIIRS
satellite data): how much artificial light brightens the night sky, in the atlas's
zones from 0 (pristine) to 7b (city). Tap a place for its zone and the sky's
brightness in mag/arcsec² (a natural sky is 22.0). The add-on downloads the atlas
for 15-35 E, 55-70 N once (about 1 MB), keeps it as an 8.6 MB file and draws the
tiles itself.

**Sunset & sunrise** rates every sunset and sunrise of the next two days from 0 to
100 (dull, fair, good, great, fiery), worked out for each place at the minute its own
sun sets or rises, between the weather model's hours. A colourful one needs three
things, and the score looks for each:

- **Cloud to light up**: high and middle cloud over you and in the part of the sky
  towards the sun, out to about 120 km. Best at about half cover. A clear sky gives a
  clean sunset with little colour (fair); an overcast high sky can still glow.
- **A way in for the light**: the reddest light reaches a cloud just before it goes
  dark there, after skimming low over the earth towards the sun. For high cloud it
  passes under 2 km some 190-410 km further towards the sun, for middle cloud some
  80-310 km, so low cloud there turns the sky grey, however nice it looks overhead
  (and a middle-level deck reaching 350-450 km away does the same to middle cloud).
  That's why the weather model reaches well beyond Finland. Where it can't see far
  enough, it counts a third cloudy and the popup says so.
- **A view**: low cloud over you hides it; fog and haze dim it; rain spoils it.

Tap the map for the score and why, when the sun sets or rises there, when the colours
are best (until the sun is 3.5° down, about 25 minutes after a November sunset in
Helsinki), and the other sunsets and sunrises coming up at that place. The time bar
steps from one to the next. Zoomed in from zoom 11, the panel looks at the terrain
(Mapzen's elevation tiles on AWS, about 30 m) for spots on land with an open view
towards where the sun goes down or comes up: 10 km out in five directions 16° either
side of the sun (the colours spread across the sky, and up here the sun slides along
the horizon as it sets), higher than their surroundings and looking over water. The
best few are marked with a wedge the way to look; tap one, or any place on land, for
its view. Treat it as a pointer to places worth checking, not a promise.

**Wind forecast** shows FMI's HARMONIE forecast over Finnish waters (the Gulf of
Finland from the Estonian islands to Vyborg Bay, the Åland Sea and the Archipelago Sea,
and the Gulf of Bothnia up to Tornio) for the next 48 hours, hour by hour on the time
bar. An arrow at each forecast point, about 30 km apart, points where the wind blows
and is coloured by speed like the weather stations; from zoom 9 the speed is written
beside it, from zoom 10 with the gust in brackets. Moving wind lines follow the wind
between the points; they pause while the map moves and **Moving wind lines** under
the switch turns them off. The forecast is fetched at most every 3 hours, only while
the switch is on. When the weather stations have been read, **Settings →
Troubleshooting** says how far the forecast is from their latest readings.

### Rain radar forecast

With the rain radar on, the time bar runs from an hour back to two hours ahead.
The frames after the newest radar picture are made by the app: it measures how
the rain moved over the last 15 minutes and keeps moving the newest picture the
same way. They are fainter and softer the further ahead, and the time bar says
"forecast" in orange. It answers "will that band reach us within the hour"; it
cannot foresee showers forming or dying, and it is less sure with every step.

## Night sky

**Night sky** at the top of the weather panel opens a star map: the whole sky as seen
lying on your back, the horizon round the edge, straight up in the middle, the way you
face (N, E, S, W) at the bottom. It shows the stars to about magnitude 5 with their
colours, the constellations, the planets, the Moon with its phase, and the radiants of
meteor showers that are on. The time slider and play button step through the night,
from sunset to sunrise; ‹ and › go a night back or forward, **Tonight** comes back.
Tap a star, planet or the Moon for its name, brightness and where to look.

It is worked out for where your phone was last seen (from the companion app), or for
the map's centre. **Tonight** lists what's worth looking at, best first: planets and
when they're highest, close pairings of the Moon, planets and bright stars, meteor
showers near their peak, the northern lights from the Kp forecast, the Moon's phase,
when it's properly dark (the sun 12° down; there's no such darkness in early summer)
and the clouds the weather model expects over the night. Tap a line to jump to that
time.

When something is really worth stepping out for (a close pairing, a big meteor shower
at its peak, a good chance of northern lights) and the clouds won't hide it all, a
small note shows at the top of the map from the afternoon on: once a night, closed
with ×. Nothing is sent to the phone. The switch at the bottom of the star map turns
the notes off. The positions are good to a fraction of a degree (planets from JPL's
approximate orbits, the Moon from the Astronomical Almanac's short series), which is
plenty for finding things by eye.

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

Open the area editor from Menu › Settings › Alerts › Edit alert areas (while you edit, a button at the top right finishes too). The first start
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
(and the switch is on), and nothing is polled while it is closed, except as follows. Rescue aircraft add one small request every 10 seconds while the panel is open and, while their switch is on, every 30 seconds while it is closed; their flights take about 6 kB of database per hour flown (a few MB a year), and the memory holds only each airborne aircraft's latest position. The history view's flights are sent to the browser as simplified lines, a few at a time, so even a year is never in memory whole. Each passage fetches the sea conditions once if they're more than 15 minutes old, and from December to April the ice is checked once a day (two small requests). Weather stations add three small requests to FMI at most every 10 minutes while the panel is open, kept in a few kilobytes. The harbour list (well under 1 MB on disk) is read into memory only while harbours are being shown and let go after 10 minutes. Automatic route planning reads only the fairway lines of the chart areas along the way and keeps the result for a few minutes only if it is small. Chart and sea mark tiles you have viewed are kept in a cache of up to 300 MB, and drawn chart tiles in one of up to 512 MB (the zoomed-out ones, about 10 MB for all of Finland, are kept apart and served as they are). Weather layers are only fetched for the layers switched on while the panel is open. The weather model behind the forecast layers is 21 requests to FMI at most every 3 hours, and the marine forecast 3 to Open-Meteo; each is kept on disk as one small file per hour (about 33 kB each, 1.6 MB a run) and only the hours being shown or worked on are read, so neither is ever in memory whole. The sunset scores are worked out on the NAS from it after each new run (a second or two), one sunset or sunrise at a time. Lightning is one request every 5 minutes and the aurora two every 15 while they're on. The light pollution atlas is downloaded once (about 1 MB) and read from its 8.6 MB file without loading it; its tiles are drawn on the NAS and kept in the weather tile cache. Weather layer tiles are kept in a cache of their own of up to 80 MB, each layer's frames for as long as that layer says (a radar frame for hours), and removed from the disk after that. Sunset spots and the star map are worked out in the browser; the terrain tiles for the spots come through the NAS and share the chart tile cache (about 100 kB each, only where you zoom in with that layer on). While the panel is hidden for a minute (phone locked, another app), its live connection closes and the panel-only fetching stops until it is seen again. The drawn chart's data is downloaded an area at a time (about 20 km across) when first viewed, refreshed after a month, and removed when not viewed for two months; the menu shows how much is stored. Outside Finnish waters an area takes one or two requests to the Overpass API (streamed and drawn into a small depth grid as it arrives, so even the Stockholm archipelago's coastline is never held in memory whole) and EMODnet's depth model is fetched for about 40 km squares (under 1 MB each, kept on disk for half a year, a few hundred kB compressed).

## Running without Home Assistant

Ferry Watch can also run as its own Docker container, straight on the NAS. It then
starts with Docker and keeps tracking, counting passages and drawing the chart whether
or not Home Assistant is running. Home Assistant becomes optional: with its address and
an access token, the container still sends alerts through the companion app, reads your
phone's position for the "only in my area" rule and publishes the ship entities.

What is different from the add-on:

- **Opening it.** At `http://<the NAS's address>:8099`, on your home network or over
  Tailscale. There is no Home Assistant login in front of it, so keep the port off the
  internet; `FW_PASSWORD` adds a password (the browser asks once and remembers).
  To keep it in the Home Assistant sidebar, add a **Webpage** dashboard with that address.
- **Settings.** The add-on's Configuration tab becomes values in `compose.yaml`, one for
  each option in the table above with `FW_` in front, in capitals: `FW_NOTIFY_SERVICE`,
  `FW_PHONE_ENTITY`, `FW_RETENTION_DAYS`, `FW_REQUIRE_PHONE_AREA` and so on.
- **Updates.** No Update button: `git pull`, then `docker compose up -d --build`.
- **Backups.** Home Assistant no longer backs it up. Everything is in the `data` folder
  next to `compose.yaml`; `ferrywatch.db`, `logbook.db` and `photos` are the parts worth
  keeping, the rest is downloaded again by itself.

| Value | What it is |
| --- | --- |
| `FW_HA_URL` | Home Assistant's address, like `http://192.168.1.30:8123`. Leave out to run with no Home Assistant at all. |
| `FW_HA_TOKEN` | A long-lived access token: in Home Assistant, your profile → Security → Long-lived access tokens. |
| `FW_PUBLIC_URL` | Where your phone reaches the panel, like `http://192.168.1.20:8099` or its Tailscale address. A tap on an alert opens it, and the alert's map picture is loaded from it. Without it alerts come with no picture. |
| `FW_NTFY_URL` | An [ntfy](https://ntfy.sh) topic address, like `https://ntfy.sh/a-long-name-nobody-can-guess`, as a second way for alerts (below). |
| `FW_PASSWORD` | A password for the panel. |

To set it up, on the NAS (with Docker and git):

1. `git clone -b phase-4-fleets https://github.com/aronwarna/ha-ferry-watch` and go into
   the folder.
2. Put the secret values in a file called `.env` next to `compose.yaml`, one per line,
   like `FW_HA_TOKEN=...` and `FW_PUBLIC_URL=http://192.168.1.20:8099`. Check the other
   values in `compose.yaml` (Home Assistant's address, the notify service).
3. `docker compose up -d --build`, then open `http://<the NAS's address>:8099`.

To bring the add-on's tracks, stats, areas and settings over, stop the container and
copy the add-on's data folder into `data`: at least `ferrywatch.db` (with any
`ferrywatch.db-wal` beside it), `logbook.db` and the `photos` folder. The add-on's data
is not reachable from Home Assistant's file editor; the simplest way is a Home Assistant
backup of the Ferry Watch app only, downloaded and unpacked (its `data` folder is
inside). While both run, switch alerts off in one of them (leave `FW_HA_TOKEN` and
`FW_NTFY_URL` out of the container), or every passage alerts twice.

### Alerts when Home Assistant is down (ntfy)

With `FW_NTFY_URL` set, and the ntfy app on your phone subscribed to the same topic, an
inner-route alert that Home Assistant can't take goes out through ntfy instead. Only
Home Assistant knows where your phone is, so these alerts are not held back by your
area. Anyone who knows the topic name can read what is sent to it, so pick a long one.
Feed warnings use the same way. Stats shows which way each alert went.

## If a ship never shows up

If a ship has been re-flagged, its MMSI changes. For the four default ships the
add-on then looks the ship up by its exact name and switches to the new MMSI
automatically (see the log). For other ships, remove the ship and add it again
by name.

## Data sources

- Ship positions: [Digitraffic](https://www.digitraffic.fi/en/marine-traffic/), Fintraffic, CC BY 4.0.
- Faulty lights and buoys: Digitraffic (Fintraffic / Väylävirasto), CC BY 4.0.
- Sea conditions, weather stations, sea ice observations, rain radar, lightning and the weather model (HARMONIE): [Finnish Meteorological Institute](https://en.ilmatieteenlaitos.fi/open-data) open data, CC BY 4.0.
- Weather model when FMI can't be reached, and the waves, sea temperature and sea level: [Open-Meteo](https://open-meteo.com), CC BY 4.0.
- Northern lights: NOAA Space Weather Prediction Center (OVATION aurora nowcast and the planetary Kp forecast), public domain.
- Light pollution: [Light Pollution Atlas 2025](https://djlorenz.github.io/astronomy/lp/), David Lorenz, from VIIRS data by the Earth Observation Group, Payne Institute, Colorado School of Mines.
- Terrain for the sunset spots: [Mapzen terrain tiles](https://registry.opendata.aws/terrain-tiles/) on AWS Open Data (in Europe mostly from EU-DEM; © the providers listed there).
- Stars and constellations: [d3-celestial](https://github.com/ofrohn/d3-celestial) by Olaf Frohn (BSD 3-Clause), from the HYG star database. Planet orbits: JPL's approximate Keplerian elements.
- Ice chart: Finnish Ice Service (FMI), via E.U. Copernicus Marine Service Information (Baltic sea ice concentration and thickness charts).
- Nautical warnings: Traficom's open spatial data service (navigational warnings), CC BY 4.0. Sea weather warnings: Finnish Meteorological Institute, CC BY 4.0.
- Bridges, locks, canals and speed limit areas: Väylävirasto, CC BY 4.0.
- Terrain map: National Land Survey of Finland (maastokartta), CC BY 4.0.
- Harbours: © [OpenStreetMap contributors](https://www.openstreetmap.org/copyright), ODbL, via the Overpass API.
- Wrecks and coastal heritage: Museovirasto (Finnish Heritage Agency) registers.
- Drawn chart outside Finnish waters: depths from the [EMODnet Bathymetry](https://emodnet.ec.europa.eu/en/bathymetry)
  digital terrain model (EMODnet Bathymetry Consortium, CC BY 4.0); coastline, sea marks, lights, routeing
  and names © [OpenStreetMap contributors](https://www.openstreetmap.org/copyright), ODbL, with
  [OpenSeaMap](https://www.openseamap.org)'s sea mark tagging, via the Overpass API.
- Drawn chart, the base map (land, islands and lakes where the chart has no data) and telling land from open sea where Traficom's depths stop short: [Natural Earth](https://www.naturalearthdata.com)
  1:10m land, minor islands and lakes (public domain).
- Aircraft: [adsb.fi](https://adsb.fi) open data (personal, non-commercial use).
- Maps, ocean map and imagery © Esri, GEBCO, NOAA, HERE, Garmin, © OpenStreetMap contributors; sea marks © OpenSeaMap;
  nautical chart and depth data © Traficom (Traficom hydrographic data
  licence, not for navigation, does not meet the requirements of an official
  nautical chart); fairway and navigation aid data © Väylävirasto (CC BY 4.0); aircraft photos © their photographers via
  Planespotters.net (loaded by your browser, never stored by the add-on).
- Ship photos: Wikipedia / Wikimedia Commons, credited on each photo.
