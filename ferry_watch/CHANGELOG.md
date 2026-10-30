# Changelog

## 0.25.0

The menu and the Stats tab rebuilt (items 1 to 18 and 31 of the UI review).

- On a computer the menu is a panel down the right side and the map stays usable beside
  it. On the phone it's a sheet of fixed height from the bottom: drag it down by the
  handle or tabs, or tap the map above it, to close it.
- The tabs are Ships, Stats, Layers and Settings, with icons. Each tab keeps its own
  scroll position.
- Weather has its own button on the map, next to the ruler. It opens a panel with big
  switches for sea conditions, weather stations, sea ice, rain radar and wind forecast,
  each with its opacity slider. The button lights up while weather is drawn on the map.
  Weather is no longer in Layers.
- Stats:
  - Four tiles at the top: this week, this month, this year and the inner share of the
    last 30 days, each compared with the same stretch of the period before.
  - Ship chips double as the colour key; tap one to leave that ship out. Below them you
    can pick both ways, to Helsinki or to Stockholm. These filters apply to everything in
    the tab and are remembered.
  - A switch between inner passages and inner share (the percentage of passages that
    took the inner route).
  - Year, Month, Week, Weekday and Time views on one row. Year only shows once there are
    two years of data.
  - The Time view replaces Hours. It has a strip for each direction in 10-minute steps,
    zoomed to the hours the ferries actually pass, with the usual time marked.
  - The chart is taller, with bigger labels and grid lines with numbers. On a computer,
    hovering a bar shows the split by ship. On a computer, Stats widens the panel to two
    columns.
  - Ship rows are two lines (inner count and share, then usual times). Tap a row to count
    only that ship, and tap it again for all of them.
  - The passage list follows what you've picked (bar, day, ships, direction). It's
    grouped under day headings with one line per passage: time, ship, Inner/Outer,
    direction, wind, and a bell if an alert went out. "Show more" adds 20 at a time.
  - The weather comparison is folded under "Does the weather matter?". It only shows
    when the selection has at least 20 passages.
  - The month calendar has arrows to the months either side.
  - Aircraft history now sits at the bottom of Stats.
  - CSV download of this selection or of everything.
- Dates read "Thu 30 Oct" with 24-hour times.

## 0.24.2

White sector wedges end where they stop being useful, not at the end of the fairway leg.

- A wedge now ends where it gets wider than the fairway's area, which is where its edges
  reach the buoys and staying in the white no longer keeps you between the marks. It also
  ends sooner if the fairway turns out of it or leaves its area.
- Where there's no fairway area to go by it stays short (about 800 m), and it's never
  longer than about 1.6 km or shorter than about 400 m.
- The chart's tiles are drawn again once after the update.

## 0.24.1

Sector lights and all-round lights drawn more like Sailmate's paper chart, from your
Porkkala and Barösund screenshots.

- A sector light's narrow white sector is a long yellow wedge, not a short flare.
  It reaches as far as the fairway it leads along: out to where the fairway's line turns
  out of the sector, so you can see a buoy sitting on its edge (at most about 2 km; about
  1 km when no fairway runs inside it). It's drawn under the lines and marks, from zoom 13.
- A sector light on a leading line (like Träsköklobb alempi) gets that wedge along the line
  instead of the flare.
- All-round lights (Toppholm, Högholmen, Svartö Höganäs and the like) are a ring in their
  light's colour with its letter above, as lighthouses already were, instead of a flare.
  Leading marks and lit buoys keep their flares.
- The chart's tiles are drawn again once after the update.

## 0.24.0

The drawn chart's land, the green boat route and a tidier menu, from your phone screenshots.

- Buildings are drawn at their true size and shape: from zoom 14 by their outline (houses
  dark like the paper chart's, big buildings lighter with a dark edge), at zoom 13 as small
  squares of the same area. Before, every house was a black square of the same few pixels
  at every zoom, which turned towns black. The Land Survey part with the buildings is
  downloaded again once after the update; until it arrives the old squares are drawn to
  scale.
- Land contours: the Land Survey gives their heights in millimetres, so the chart never
  noticed that 5000 was 5 m and drew every contour at every zoom, labelled "5000". Now
  zoom 12 shows every 20 m, 13 every 10 m, 14 every 5 m and closer in all of them, in a
  fainter brown, with labels in metres. The land is light again.
- The small-craft trunk route's green band is Sailmate's softer green instead of the
  bright one.
- Soundings from zoom 11, one every 60 pixels or so, like Sailmate (card T8).
- Menu › On the map is grouped into Weather, On the chart and Traffic, and each switch's
  explanation folds to two lines (tap it to read it all). A source that can't be reached
  still shows its whole message.
- Settings is in four boxes: Alerts, Drawn chart, Troubleshooting and About. A saved Land
  Survey key folds away behind "Change", and the data sources fold under About.
- Alert areas are edited from Settings › Alerts › Edit alert areas; the button for them
  left the top bar (it shows there only while you edit, to finish).
- The menu's tabs stay at the top while you scroll it.
- The credit line at the bottom starts with the drawn chart's own sources.
- The chart tiles are cut again once after the update (tile version 18).

## 0.23.1

Even traffic lanes when zoomed out (card T7).

- At zooms 8 to 10 the traffic separation lanes and zones had darker vertical and
  horizontal strips where two chart blocks meet: each block keeps a
  lane a little past its own edge, and both copies were tinted on top of each other. Each
  block's copy is now cut at the block's edge, so the tint is even all along the lane.
- The lane arrows are placed over the whole lane, not once per block, so one can sit
  across a block edge and none is drawn twice.
- The chart tiles are cut again once after the update (tile version 17).

## 0.23.0

The wind forecast over the map (card T5).

- New switch "Wind forecast" under Menu › On the map. It shows FMI's HARMONIE forecast
  for the Gulf of Finland, from Hanko and the Estonian islands to Vyborg Bay, for the
  next 48 hours: an arrow every 30 km or so pointing where the wind blows, coloured by
  speed like the weather stations (the number beside it from zoom 9, the gust in
  brackets from zoom 10), with fewer arrows when zoomed out.
- Moving wind lines follow the wind between the arrows, coloured the same way, at 30
  frames a second. They pause while you pan or zoom and while the page is hidden, and
  "Moving wind lines" under the switch turns them off. The opacity slider works on both.
- The time bar steps through the forecast hours; play runs through the two days.
- One forecast is fetched at most every 3 hours, only while the switch is on, and kept
  on the NAS as a 100 KB file. Memory: about 3 MB more while it is in use, nothing once
  the panel closes.
- When the weather stations have been read, Settings › Troubleshooting says how far the
  forecast is from their latest readings, so you can see that the arrows agree.
- If FMI is down it shows with the switch and in Settings, and nothing else is affected.
- In the simulator the wind switch replaces "Wind forecast (test)", with made-up FMI
  answers that roughly follow the simulated stations.

## 0.22.0

The rain radar over the map (card T4).

- New switch "Rain radar" under Menu › On the map. It shows FMI's Finnish radar composite
  for the last hour in 5-minute frames, at 70% opacity to start with. It opens on the
  newest frame; press play on the time bar and the hour plays at two frames a second.
- It uses FMI's new radar layer (radar_finland_cappi_dbzh), which replaces the old ones
  FMI retires in November 2026. From October to April it uses FMI's winter colours, which
  show light snow; in summer the summer colours.
- Frames are kept on the NAS for 6 hours and removed from the disk within half an hour
  after that, whether or not anyone is looking. If FMI is down it shows with the switch
  and in Settings, and nothing else is affected.
- In the simulator the radar switch replaces "Rain (test)", with drawn rain cells.

## 0.21.0

The Baltic sea ice chart over the map (card T3).

- New switch "Sea ice" under Menu › On the map, from November to May. It shows FMI's
  daily ice concentration chart from Copernicus Marine, blue for thin cover to white for
  solid ice, at 60% opacity to start with. It opens on the newest day; the time bar
  steps back through the last two weeks.
- Days are kept on the NAS for 15 days in the overlay cache. If the service is down it
  shows with the switch and in Settings, and nothing else is affected.
- In the simulator the ice switch is there all year, with drawn ice.

## 0.20.0

The groundwork for weather layers over the map (card T2). Nothing new shows on your
map yet: the sea ice chart, rain radar and wind forecast plug into this next, each
as a small step.

- Each weather layer gets a switch under Menu › On the map, with an opacity slider
  once it's on. Both are remembered by each phone or browser on its own.
- A time bar above the ship chips plays, pauses and steps through a layer's frames,
  or you drag it to a time. It starts at the newest frame and keeps to the newest as
  new ones arrive. With several layers on, it steps through all their times and each
  shows its own frame for that moment. The next frame loads out of sight while one
  is showing, so playing doesn't flicker; a slow connection makes it wait rather than
  skip. The bar gives way while a ship card, a replay or the area editor is open.
- On the NAS, layer pictures are kept in a cache of their own (up to 80 MB, apart
  from the chart's), each layer for as long as it says: radar frames for hours, not
  the chart's month, and then removed from the disk. Only the layers switched on are
  ever fetched, and only while the panel is open.
- A layer whose source can't be reached is logged once, says so under its switch and
  in Settings, and is tried again a few minutes later; the rest of the app carries on.
- Memory in the simulator: unchanged with the layers off (62 MB with the panel open,
  as before); 3 to 4 MB more with two test layers playing.
- Settings: switching off the Swedish and Estonian side saves about 5 to 10 MB since
  0.19.0, not 40 MB; the text says so now.

## 0.19.1

- Drawn chart: tiles are sharp again, drawn at your screen's full pixel density as
  before 0.19.0. The other speed and memory changes of 0.19.0 stay. In the
  panning test that's 55 frames a second with 8 stutters (0.18.1: 54 and 18;
  0.19.0: 60 and none), and the phone's browser holds about 70 MB of chart
  pictures again instead of 37 MB. The NAS side is unchanged.

## 0.19.0

Performance and memory (card T11). Measured in the simulator with the Swedish and
Estonian side on, going through Porkkala at zooms 8 to 15, Tallinn, Åland, Stats and
the aircraft history:

| | Before | After |
| --- | --- | --- |
| Most memory while a new area fills in | 126 MB | 91 MB |
| Memory after those views, area seen before | 89 MB | 66 MB |
| Panning on a phone-sized screen | 54 frames/s, 18 stutters | 60 frames/s, none |
| Chart pictures held by the phone's browser | 70 MB | 37 MB |

- Settings now says where the memory goes: the chart, the foreign depth model, the
  database, ships, aircraft, and the program itself (Python and its libraries, most
  of it), and the most it has used since it started.
- The chart's parsing, building and cutting now share two worker threads instead of
  up to six. Python only runs one at a time anyway, so it's as fast, but fewer areas
  are half built at once, and their memory peaks no longer stack.
- Drawn chart tiles you've seen are kept on the NAS (up to 64 MB), so a view seen
  before is sent straight from disk, already compressed, without reading its chart
  areas into memory or drawing the tile again. A tile is drawn again once an area
  under it is downloaded again.
- The depth model for the Swedish and Estonian side is only kept in memory while
  areas are built from it (and half a minute after).
- The picture library (Pillow, about 4 MB) is only loaded when an alert picture is
  drawn. The database is held to 2 MB of cache and 8 MB in all, sorting on disk.
- Panel: chart tiles are drawn at twice the screen's pixel size at most (your S22's
  screen is three times): under half the pixels to draw and keep, barely softer. The
  chart's lettering is only placed again when a pan has ended. While the panel is
  hidden (phone locked, another app) it stops asking for updates, and after a minute
  closes its live connection, so the NAS stops fetching weather, your phone's
  position and aircraft for it; it catches up as soon as you look again.
- Clock times in the panel are formatted with one shared formatter (making a new one
  for every time shown was the panel's busiest function while ships moved).

These are simulator numbers on a PC (glibc). Home Assistant's add-ons run on Alpine
(musl), which hands freed memory back more readily, so the NAS starts lower: there
the gain is smaller (about 5 MB at the peak in a musl-like test run, 7 MB after the
views). The new line in Settings shows the real split on your NAS.

## 0.18.1

Fixes from the NAS log of 23 to 25 October:

- Drawn chart, Archipelago Sea and Åland: about 25 areas there never got a
  chart, because a single Traficom depth area (the open water around
  thousands of islands) is more than 4 MB on its own. Such an area is now
  read piece by piece, keeping only the parts that reach into the area being
  drawn, so it no longer needs the whole thing in memory.
- Land Survey (lakes, rocks, names, land detail): your NAS hasn't been able
  to connect to it at all since 23 October. Ferry Watch now asks again after
  a quarter of an hour, then half an hour, and so on up to every 6 hours,
  instead of three times in a row every hour, and says so in the log.
- Water traffic signs: when Väylä's server is busy (it answered a burst of
  requests with "403 Forbidden"), the request is tried again after a short
  pause instead of waiting an hour.
- Swedish and Estonian side: OpenStreetMap's "502 Gateway Timeout" (server
  at capacity) is asked again once a little later, like its other busy
  answers.
- Submarine cables: Traficom doesn't publish them yet (its open data entry
  says "not available for now"), so none show. They'll appear on their own
  once Traficom adds them.

## 0.18.0

- Drawn chart, Swedish and Estonian side: the shallows are now the same
  blues as on the Finnish chart, without the grey speckle. The depth model
  is smoothed first, specks smaller than the model can really show are left
  out, and the depth bands are painted one over the other so no thin gaps
  open between them (each gap used to get a black coastline edge, which
  made the grey look). Tiny islets only show once zoomed in far enough to
  see them as islands rather than dots.
- The foreign side fills in faster: OpenStreetMap is asked once for every
  2 x 2 chart blocks instead of twice per block, the downloads run while
  other blocks are being built, building a block takes about a third of
  the time it did, and the blocks nearest the middle of the screen are
  built first.
- While a chart tile is still loading, the map underneath now shows
  through instead of a white square.
- Chart blocks outside Finland are built again the next time you look at
  them (once), to get the new look.

## 0.17.1

- Drawn chart: leading lines are now dashed through their marks and on out
  to sea, and solid only where they are the fairway, as the key draws them
  ("firm line is fairway"). The red bearing sits beside the solid part.
- Spar buoys and ice buoys can now lean, as on the paper chart, to keep
  clear of each other and of the fairway line. They lean at most 50° from
  upright, and each one keeps its lean at every zoom level.
- The chart is recut from what's already on the NAS. Nothing is downloaded
  again.

## 0.17.0

- Drawn chart: every sea mark now follows Traficom's symbol key
  (merkkienselitys), on the Finnish and the foreign side alike.
- Leading lines are dashed behind the front mark and solid from it out to
  sea, with the bearing in red beside them ("356,9° — 176,9°"). Leading
  lights only show their yellow flare along the line, with no sector arcs.
- Sector lights: thin coloured arcs, dotted sector limits and the colour
  letters outside the arc. A narrow white sector is a yellow wedge.
- Directional lights get a flare and a solid black line along the beam,
  with its bearing in red.
- A lighthouse is a black six-pointed star with a ring in its light's
  colour up close. A racon on it is written under the light's character.
- Spars, buoys, edge marks, leading beacons, cairns, beacon towers and radar
  marks are redrawn to the key's shapes and sizes. Rocks, wrecks, foul
  ground, the pilot station, anchorages, pipelines, the boat-route band and
  draught figures were also touched up.
- The chart is recut from what's already on the NAS. Nothing is downloaded
  again.

## 0.16.6

- Drawn chart: the depth contours added in 0.16.5 are gone again. They ran
  along fairway edges where the paper chart has no line, so contours now end
  where Traficom's data ends them, as before. Chart blocks saved by 0.16.5 are
  downloaded again in the background; until a block is, it can still show
  the extra lines.
- Island names showing ahead of lake names (from 0.16.5) stays.

## 0.16.5

- Drawn chart: depth contours no longer stop suddenly. Where the official
  data leaves a contour out (often the 20 m line, where depth areas from
  different chart scales meet), it's now drawn from the edge between the
  depth areas. The chart already on the NAS is fixed in place after the
  update, without downloading it again.
- Drawn chart: island names show sooner. A lake or marsh on an island (like
  Träsket on Stora Träskö) no longer takes the island's label; its name only
  shows from zoom 15.
- Zoomed-out chart checked against long-press samples: shallows and islands
  are all there at zoom 8 to 12. The white squares while panning are tiles
  still loading.

## 0.16.4

- Settings › Troubleshooting: "Save chart tile on long press". With it on, a
  long press on the drawn chart saves what the chart has there (the tiles at
  zoom 8 to 12 and the depth areas under the spot) as a file in Home
  Assistant's Media folder, under ferry_watch › chart-samples, with a download
  link. It's for sending in when the chart is drawn wrong somewhere, such as
  shallows and islands missing when zoomed out. The last 10 files are kept.

## 0.16.3

- Drawn chart: no more tan "land" out in the open sea. Out by the sea border
  south of Porkkala and Helsinki, the Finnish waters beyond Traficom's depth
  data are now drawn as open water (only islands there are land), and dredged
  and unsurveyed areas are water too instead of land with a coastline round
  them, from zoom 8 on.
- A dredged area is coloured by the depth it is dredged to, with a dashed
  limit; an unsurveyed area is white with thin blue lines and a dashed black
  limit, as in Traficom's symbol key.
- The routeing part of the chart is downloaded once more, in the background,
  to pick up dredged depths.
## 0.16.2

- Settings: chart problems name only the service, so the long web address
  no longer makes the menu scroll sideways.

## 0.16.1

- Chart download no longer stalls for hours when the Land Survey service
  doesn't answer: after a timeout its layers are skipped for 15 minutes and
  fetched again later, so the Traficom chart keeps downloading.
- Swedish and Estonian side: reads EMODnet's depth model even though it comes
  without a georeference ("no georeference").
- Faulty lights and buoys load again (Digitraffic refused a parameter).

## 0.16.0

- **Swedish and Estonian side of the drawn chart**, in the same style as the
  Finnish side with no visible seam: depth areas and contours from EMODnet
  Bathymetry, coastline, buoys, beacons, lights with sectors and characters,
  traffic separation lanes, leading lines, rocks, wrecks and names from
  OpenStreetMap / OpenSeaMap. Finnish data always wins where both exist.
  Less detailed than the Finnish side (no soundings, fairway areas or land
  detail abroad).
- Settings → "Swedish and Estonian side" turns it off to save about 40 MB of
  memory.

## 0.15.0

- **Drawn chart closer to the paper chart** (Sailmate):
  - names in mixed case like the paper chart: towns upright serif, islands
    and land upright, waters sloping; larger; long names hyphenated and
    Finnish and Swedish names stacked;
  - rock symbols as on the paper chart (under water, at the surface, above
    water, rock clusters with one sign each);
  - larger buildings and mast heights; leading-mark boards along their line;
  - submarine cables (magenta wavy line) and pipelines;
  - soundings spaced so they don't run together, and no longer cut in half at
    tile edges;
  - zoomed out, no more thin lines or channels across land at area edges.
- The watch zones are drawn as outlines only, so they no longer tint the
  chart around Porkkala.

## 0.14.1

- Chart download: a dropped connection ("Connection reset by peer") is now
  asked again twice before an area counts as failed.
- The download no longer stalls when one part of an area fails: it moves on
  to the next area, tries the failed one again an hour later, and only pauses
  when several areas fail in a row. Settings shows when the last area
  finished and how many are waiting for another try.

## 0.14.0

- **Ship card on phones** is now a sheet: drag it down to a small banner with
  the ship's name and picture, tap the banner to open it again. "Show whole
  track" folds it down by itself so the track is visible.
- **Track playback** like MarineTraffic: play the selected ship's voyage or a
  passage from Stats, with a time slider, speeds from ×60 to ×3600 and the
  track coloured by speed.
- **Aircraft history**: rescue, police and Border Guard flights are now stored
  while the aircraft switch is on. A new view shows the last 24 h, 7 days,
  30 days or year as glowing lines or a heat map, with a time-lapse and a tap
  on any flight for its times, altitude and distance.
- A tapped aircraft now shows its whole flight as a thick line coloured by
  altitude.
- **Weather per passage**: each passage stores the wind at Mäkiluoto (speed,
  gusts, direction), the water temperature and, December to April, sea ice
  at Porkkala (FMI ice thickness and the Copernicus ice chart). Stats shows it
  on each passage and in a new Weather section comparing inner and outer
  route by wind strength, wind direction and ice. Tap a calendar day to list
  its passages.
- Sailings on the outer route are now counted when a ship crosses Porkkala's
  longitude without taking the inner route, so the comparison has both sides.
  They don't send alerts.

## 0.13.0

- **Drawn chart, closer to the paper chart**: labels are now placed across the
  whole screen so names, light characters and depth figures no longer sit on
  top of each other; paper-chart buoys, beacons and bold light flares; major
  lights' characters from zoom 10; heritage names only when zoomed in.
- **Traffic separation schemes** (lanes with direction arrows, separation
  zones and lines, precautionary areas, deep-water routes), pilot boarding
  places, radio calling-in points, VTS limits and water traffic signs.
- **Land detail** with the Land Survey key: height curves, lakes, roads,
  railways, buildings, masts, chimneys and wind turbines.
- The drawn chart now works zoomed out (from zoom 8) and along the whole
  Finnish coast, Gulf of Bothnia and Archipelago Sea included.
- Fixed the black areas over the sea in the zoomed-out raster chart.
- **Route tool** (ruler button): tap to measure and plan routes in nautical
  miles with bearings and ETA, save them, and **Auto** to plan along the
  marked fairways for your draught. For planning only, not navigation.
- **Weather stations**: live wind from all FMI coastal stations on the map,
  with gusts, temperature and a 24 h graph.
- **Harbours** from OpenStreetMap with their services.
- Uses less memory: smaller chart caches and download pages, parsed chart
  data released after 5 minutes idle.

## 0.12.0

- **Drawn chart restyled** to look like Traficom's scanned chart: depth tints
  and a crisp coastline, dotted 3 m and dashed 6 m contours, deep-water routes
  in grey with the green route band, draughts like "(9,0)", red leading-line
  bearings, black aid silhouettes with topmarks, light flares and sectors,
  light characters such as "Fl(2)WRG.10s", speed limits and restricted areas,
  and chart-style names.
- **Wrecks and coastal heritage** from Museovirasto's registers: wrecks and
  underwater sites, plus bunkers, trenches and gun positions on the coast and
  islands (from zoom 13). Tap one for its dating and a link to the register.
  hylyt.net isn't open data, so it isn't used.
- **Next passage**: each ferry's chip and card show when it reaches the
  Porkkala lines and how often it has taken the inner route lately; there is
  also a `sensor.ferry_watch_next_passage`.
- **Sea conditions**: wind, gusts, visibility, sea temperature and sunset from
  FMI in a pill under the top bar, and in the alert text.
- **You are here**: your phone's position on the map and the distance to each
  ferry.
- **Other ships** nearby (off by default), **faulty lights and buoys** on the
  chart, and new groups: Eckerö Line, Suomenlinna ferries, cruise ships,
  pilot boats and tugs.
- Menu reorganised into Ships, On the map, Stats and Settings, with the memory
  in use shown under Settings. Better layout on phones.
- Much less memory for the drawn chart: chart data is stored compactly with
  three levels of detail, so the worst case dropped from about 1 GB to under
  80 MB. Existing chart files are converted once on first use.

## 0.11.3

- The ship cards along the bottom respond to a click anywhere on them. They
  were rebuilt on every position report, so a click often landed on a card
  that had just been replaced.

## 0.11.2

- Drawn chart: no more "Map data not yet available" around the chart. Esri's
  ocean map has no detail past zoom 10 in the Baltic, so the ordinary map
  takes over from zoom 11, and it shows faintly through the land for roads
  and towns.
- Fewer names until you zoom in: towns from zoom 11, islands from 12, bays and
  sea areas from 13, other land names only from 15. Zoomed out (zoom 11) only
  lit marks are shown, as on overview charts.

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
