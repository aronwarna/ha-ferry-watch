# Changelog

## 0.53.0

- Route tool: pick your **boat** (Buster M, Buster XL, an 8 m aluminium cabin boat, or a
  35 ft sailboat with 1.9 m draught). The boat sets the depth it needs, its cruising speed
  and its own wind, gust and wave limits for Boating weather. Draught and speed can still
  be changed by hand.
- New route mode **Automatic**, next to Straight and Fairways: the app plans a way over
  water deep enough for the boat, on or off the marked fairways, round islands, rocks and
  shoals, keeping off the shore and slowing down in speed limit areas. Water where the
  chart only gives a depth range that may be too shallow (0–3 m for a small boat) is
  avoided where it can be, and drawn amber and dashed where the route does use it.
  Planning only: check the route on the chart.
- **Sailing routes from the wind forecast**: for the sailboat, Automatic works like race
  weather routing. It reads the wind hour by hour and works out the **Fastest** route and
  a **Comfortable** one (less beating, less strong wind and waves). Both are drawn, with
  the time each takes; tap the other one to take it. Beating stretches are dashed. Tap an
  hour in Boating weather to plan for leaving then.
- The route is now **coloured by the expected conditions**: a green, amber or red band
  under the line for the hour you picked in Boating weather.
- **Speed limit areas** are shown on the map while you plan a route (red dashed outline
  with the limit), and can be left on with a new switch in Layers.
- Long trips: Automatic plans well past 50 NM in one go (150 NM and more; the first plan of a long leg can take a minute), and the speed
  limits are checked on long routes too.
- "Boat and planning numbers" at the bottom of the route panel lists every number used.

## 0.52.0

- The sea ice layer no longer covers the islands: it is cut to the water, like the wave layer.
- Rain radar: frames now melt into each other while playing instead of jumping.
- Rain radar forecast: the time bar carries on two hours past the newest radar picture. FMI
  has no open radar forecast, so the app makes one by moving the newest picture on the way
  the rain has moved in the last quarter of an hour. The frames get fainter and softer the
  further ahead they are, and the time bar says "forecast". Showers that form or die are
  not foreseen.

## 0.51.1

- Nautical warnings could not be read ("404 Not Found"): they were removed from Fintraffic's
  Digitraffic this year and are now published by Traficom. They are read from there now.
  The popup also shows every detail the data carries under "All details".

## 0.51.0

- The inner route outlook now checks itself. Every chance it gives for a coming sailing is
  written down beforehand and marked right or wrong once the ship has passed. Stats shows
  the scorecard under the outlook: forecasts checked, inner passages called, false alarms,
  how it compares with each ship's usual share, month by month and by ship.
- It adapts with time: recent months count more when it learns, its percentages are
  corrected from the scorecard once 40 forecasts are checked, and the scorecard decides
  whether its chances are shown at all. Expect a few weeks before there is enough to see.

## 0.50.0

- **Terrain**: a fourth map type next to Map, Satellite and Chart, the Land Survey's
  topographic map (maastokartta) with contours, paths, cliffs and place names. Finland only.
- **Nautical warnings** (On the map › Warnings and waterways): Fintraffic's warnings to
  mariners, such as firing exercises, fairway works and marks missing or moved, as amber
  diamonds with their line or area dashed. Tap one for the text and how long it holds.
- **Sea weather warnings**: FMI's wind, wave and icing warnings for the sea areas, tinted
  yellow, orange or red. A warning for your own waters always shows as a warning sign with
  the wind speed in the conditions pill, and in full when you tap the pill.
- **Bridges, locks and canals**: Väylävirasto's opening bridges, locks and lock canals with
  clearance, width, length and depth where the data has them.
- **Speed limits on a planned route**: the route tool lists the speed limits the route runs
  through (km/h and knots, for how far), marks them on the route, and says about how many
  minutes they add at your speed.
- Saved routes can be downloaded as **GPX** for a plotter, and GPX routes or tracks can be
  imported (On the map › Routes).

## 0.49.0

- The panel opens faster: its own files are sent compressed (about 0.4 MB instead of 1.3)
  and kept by the browser until the next update, so a second open fetches none of them.
  Stats is sent compressed too, and the star catalogue only when the sky map is opened.
- **What's new**: after an update, a small card lists what changed since the version this
  browser last saw.
- Settings is shorter: each box folds, and only Alerts starts open. Which ones are open is
  remembered.
- What is switched on (layers, weather layers, Stats choices, boat size and speed for the
  route tool) is now kept on the NAS, so the phone and the desktop agree. The map type,
  the open tab and unsaved routes stay with each device.
- Settings › Troubleshooting › **Copy diagnostics**: a text report of the version, memory,
  every data source and the latest warnings, to paste when something looks wrong. Keys
  and passwords are left out.
- Alerts in Settings name your phone as Home Assistant does instead of showing only its
  entity id, and the first areas lose the "(from old setup)" in their names.
- Stats says "Loading the passages…" instead of opening blank, and offers to try again if
  they can't be loaded.
- The "Tonight" sky note no longer covers the open menu.

## 0.48.0

- Ferry Watch can now also run as its own container, outside Home Assistant, so tracking
  and stats carry on when the Home Assistant VM is down. Nothing changes for the app
  inside Home Assistant: this is a second way to run it, set up by hand (see "Running
  without Home Assistant" in the documentation).
- As its own container it reaches Home Assistant over the network for phone alerts, your
  phone's position and the ship entities, and can ask for a password.
- New, in both: a second way for alerts, through ntfy. When it is set up and Home
  Assistant doesn't answer, the alert goes out through ntfy instead (without checking
  where your phone is, which only Home Assistant knows). As an add-on this is off.
## 0.47.1

- Stats › Times past Porkkala: the "Show the lines on the map" link turns into "Hide the
  lines from the map" while they're shown, so the same link takes them off again (the
  "Passage lines shown" pill on the map still does too, and the link follows it).

## 0.47.0

- New **Ice chart** layer (Weather › On top): FMI's daily ice chart drawn on the map in
  the ice chart's colours, drift ice from very open (1-3/10, green) to very close
  (9-10/10, red) and grey for fast or consolidated ice, with the thickness in cm on the
  ice from zoom 7. It is cut to the sea like the other sea layers.
- Pick any day of this or last winter under its switch, or Newest / Last winter. Until
  this winter's ice comes, last winter's chart from late February is shown, and the
  panel says so.
- Tap the ice for its class, concentration and thickness.
- The chart is read from the Copernicus Marine Service's open map service (FMI's ice
  analysis as concentration and thickness), about 50 small requests for each day that's
  looked at. Each day is kept as two small pictures (the last 40 days kept).

## 0.46.0

- New weather layers: blue-green algae, from the Finnish Environment Institute's (SYKE) open
  data. Switch them on in the weather menu.
- "Blue-green algae (satellite)": SYKE's daily surface algae map from Sentinel-3 (the one
  behind TARKKA and vesi.fi), 300 m squares over the whole Baltic: pale yellow where algae
  are possible, orange probable, red certain, and a faint blue where the satellite saw clear
  sea (no colour means cloud). It's cut to the drawn chart's sea, on every map. Tap the sea
  for the class that day. The time bar steps through the days the satellite saw.
- "Blue-green algae (reports)": what people saw on the shore and out at sea, sent to SYKE
  through vesi.fi and Järviwiki, and SYKE's national monitoring sites (ringed dots): green
  none, yellow some, orange plenty, red very much. Each day shows that day's reports and
  the two before, the older ones fainter. Reports on lakes are left out. Tap a dot for
  when it was seen, by whom (site or person) and their note.
- Blooms come in summer: from October to May both layers show the last season (June to
  September), so last summer can be looked through now. The bar shows months and dates
  for them instead of clock times.

## 0.45.0

- Port times on the ship card: for each ship, the port it is in or just left, the next port
  and the one after, with when it arrived or left, and when it is due and leaves. Expected
  times are blue with a ≈ and a tag saying where they come from:
  - "port": what the ship's agent has announced to Helsinki, Mariehamn, Långnäs or Turku
    (Portnet port calls from Digitraffic, fetched every 10 minutes);
  - "usual": the ship's usual crossing time and stay, learnt from its own earlier trips
    (this is how Tallinn and Stockholm get times: Portnet only covers Finnish ports);
  - "AIS": the crew's own ETA, when it is close to the usual time.
  The berth (Olympia, Katajanokka, Länsisatama...) is shown when announced. The earlier
  trips are read once from the tracks already kept, so the usual times work from the first
  start; fleet ships learn theirs as they sail.
- Other ships' popups show their announced calls at Finnish ports too.
- Tapping a ship zooms to its track again (broken since 0.36.1), and passage replays fit
  the hour either side of the passage as meant.

## 0.44.0

- Stats: new "Inner route outlook" at the top. For each ferry sailing expected in the next
  48 hours it gives the chance of the inner route past Porkkala, with the wind, gusts and
  waves forecast for that time and what pushes the chance up or down (wind strength, wind
  direction, waves, ice, the ships' recent choices). The bar's mark is the ship's usual
  share that way.
- It's an experiment, and it says how well it does. A small model learns from your own
  passages and the weather at each one, and is tested as if it had been running all
  along: each day of the history is forecast only from what was known the midnight
  before, then scored against what the ships really did, next to the plain guess "the
  ship's usual share". "How good is it?" shows the result: inner passages caught, false
  alarms, and how often inner really followed when it said 0-10%, 10-25% and so on. If
  the model does worse than the usual share, the usual share is shown instead.
- The weather for past passages and for the forecast comes from the same place
  (Open-Meteo's hourly wind off Porkkala and its wave model, CC BY 4.0): two small
  requests every 3 hours, older days filled in once. The next sailings are read from each
  ship's rhythm; a ship under way uses its next-passage time.

## 0.43.0

- Boating weather in the route tool: each hour of the next two days scored good, okay or
  bad as a time to set off on the route, from the wind, gusts, waves, rain, visibility and
  daylight where the boat would be at each moment of the trip (at the speed in the panel).
  Pick the boat size (small open boat, small boat, cabin boat) for the limits. The line at
  the top gives the best windows; tap an hour for why, and the okay or bad stretches show
  as dots on the route. Saved routes get it when opened with Edit. The forecast is asked
  for along the route itself (FMI HARMONIE, Open-Meteo marine), so it's finer than the
  weather layers' grid; waves are counted lower among the islands, where the wave model
  sees only the sea outside.

## 0.42.0

- No-go seasons: bird and seal sanctuaries and reserves that are closed part of the year
  (no landing or no access) are drawn in red dashes with "No landing till 31 Jul" while
  they're closed, on the drawn chart from zoom 11 (and with "Highlight nature reserves" on,
  on any map from zoom 10). Tap one for its card: "Closed now: … till …" in red, or the
  dates it's closed every year and "open now".
- Seasons come from SYKE's register of Metsähallitus' movement restriction areas (the layer
  is looked up in SYKE's map service once a day and named in the log), and from
  OpenStreetMap: dated access rules, OpenSeaMap restricted areas for wildlife and dates
  written in a reserve's description ("Maihinnousukielto 1.4.–31.7.").
- Layers › Highlight nature reserves shows how many are closed now. Reserves are
  downloaded again once after the update.

## 0.41.0

- New Logbook tab: your own trips from the Garmin plotter. Save the track to a microSD card
  as GPX (Info › User Data › Data Transfer › Save to Card) and upload the file, several
  files, or a .zip of the card's Garmin folder. The plotter's one long active track is cut
  into trips where the boat lay still for 20 minutes or the log stopped; each shows the
  date, from and to (the nearest harbour), distance, time underway, average and top speed,
  and the depth and water temperature range when the sounder was on. Totals per year on
  top. Upload the same card as often as you like: only new stretches are added, and a trip
  that was half on an earlier card becomes whole.
- Tap a trip for a speed and depth profile, a name and note, or Show on map: the track is
  drawn coloured by speed (green dot where you left, red where you arrived); tap the line
  for the time, speed, depth and water temperature there. The pill at the top takes it off.
- Saved tracks (which Garmin keeps without times) are listed with their distance. ADM, FIT
  and sonar (.RSD) files are answered with what to do instead. Removing a trip keeps it out
  even if the same card is uploaded again. The logbook is in Home Assistant's backups.

## 0.40.0

- Waves, sea temperature and sea level are drawn only over the sea, on every map (Satellite
  too), instead of as a blocky box over the land. They're cut to the drawn chart's water:
  Traficom's depth areas, the Swedish and Estonian side's, Natural Earth's open sea, less
  the lakes. The add-on sends each chart tile's water alone (a few kB, at a zoom coarser
  than the map's), and the colours now reach every bay and harbour. The swell lines and the
  numbers keep to the water too, and a tap on the land gives no sea value. Where the chart
  is still downloading, the sea there stays clear until it comes.
- Weather stations: a station that doesn't measure the wind (only the air temperature)
  showed "NaN°". It now shows its air temperature in a grey tag (sea temperature and waves
  stay teal), and one with nothing to show isn't drawn.

## 0.39.3

- Ship photos from Wikimedia Commons and Wikidata showed only a strip of their top: the
  download kept just its first chunk. They're now read whole, and the cut-short ones are
  fetched again by themselves.

## 0.39.2

- Satellite view: zooming all the way in no longer turns the picture into grey "Map data
  not yet available" squares. Esri's imagery ends at zoom 17 or 18 in many places here;
  the view now asks Esri how deep its pictures go where you are and enlarges the last
  real one beyond that.

## 0.39.1

- Nature reserves whose outline in OpenStreetMap isn't closed (a missing piece or a gap
  between two pieces, as with the one round Västerlandet off Porkkala) are now highlighted
  too: the open pieces are joined end to nearest end and closed. Before, they showed only
  as the chart's dashed border and couldn't be tapped. The reserves are downloaded again
  once after the update. Any that still can't be filled are named in the log.

## 0.39.0

- Drawn chart, zoom 8 (the middle overview zoom) is made the way zoom 7 is: from zoom 9's
  tiles, each halved into its quarter. The squares there are gone: in the Archipelago
  Sea zoom 8 showed whole chart blocks white or light blue (zoom 9, from the same data,
  was right). It also shows what zoom 7 does: the lighthouses but not every sector light,
  and only the bigger contours, so it is no longer more cluttered than both zooms beside it.
- Zooms 10 and 11: lit buoys and beacons that crowd each other (along a fairway, round a
  harbour) are thinned to one in each small square of the map, the most important one,
  and they give way to a lighthouse or sector light right beside them. A lone lit buoy
  out at sea stays, and from zoom 12 every lit mark shows as before.
- Zooms 10-12 leave out the smallest contour rings round single shoals (zoom 10 under
  about 12 px across, 11 under 7 px, 12 under 5 px). The light blue still shows every
  shoal under 10 m, and from zoom 13 every contour is drawn.

## 0.38.0

- Nature reserves: on the drawn chart (zoom 11 and in, where their green dashed borders
  show) tap inside a reserve or national park to fill it with green and see its card: names
  in Finnish and Swedish, what kind of area it is, size, year protected, who manages it,
  access or landing restrictions, register id, and links (its website, luontoon.fi for
  state areas, Wikipedia, Wikidata, OpenStreetMap) plus "Show all of it". Where areas
  overlap the card lists the others to switch to. Tap again (or elsewhere) to clear it.
  Buoys, lights and wrecks still win the tap.
- Layers › "Highlight nature reserves" tints and names every reserve in view from zoom 10,
  on any map.
- From OpenStreetMap (Finland's reserves there come from SYKE's register), downloaded per
  area of about 22 km when first looked at and kept two weeks; left out of backups.

## 0.37.1

- Satellite view: Esri's satellite images again by default. The Finnish and Estonian
  aerial photos from 0.35.0 are now a switch, Layers › On the satellite view › Aerial
  photos (Finland, Estonia), off unless you turn it on.

## 0.37.0

- Sunset & sunrise worked out properly: besides the high and middle cloud to light up
  (now over you and in the part of the sky towards the sun), it checks the light's way
  in. The reddest light skims low over the earth 80-450 km towards the sun before it
  reaches the clouds, so low cloud there turns the sky grey. Also new: each place's
  own sunset minute (between the model's hours), haze and fog, a middle-level deck
  that reaches the horizon, and softer handling of a passing shower.
- Tap the map for why: "Great sunset · 75/100", when it sets or rises there and when
  the colours are best (until the sun is 3.5° down), what makes or spoils it, and the
  other sunsets and sunrises coming up at that place. The legend reads dull to fiery,
  and the panel gives the score at the map's centre.
- Zoomed in: the open-view spots check 10 km out in five directions 16° either side of
  the sun (was 6 km in three), show a wedge the way to look, and tapping any place on
  land tells its view (open, horizon height, water that way, height).
- The weather model now reaches from southern Sweden and Latvia to Karelia
  (1287 points, 21 requests a run; the Open-Meteo fallback uses every other point).

## 0.36.4

- Stats › Times past Porkkala now times every passage at one fixed place per route, to the
  second between two position reports. Inner-route passages are timed where the ship goes
  between Utterböte and Östra Styrskär off the Porkkala cape; outer-route passages where
  the ship crosses 24.40° E (between 59.55° and 60.15° N). An inner sailing that misses
  that gap is timed at 24.40° E instead. The Time view and the usual times use these too.
  Before, an inner passage was timed where it first crossed your inner-route line, up to
  a quarter of an hour away, so inner dots looked early.
- Passages from the last 60 days are re-timed from the stored tracks once after the
  update; older ones keep their own time.
- "Show the lines on the map" under Times past Porkkala zooms to both places and labels
  them (your inner-route lines are shown too); the "Passage lines shown" pill hides them.

## 0.36.3

- Ship photos: Wikimedia Commons is also searched for files that mention the ship's IMO
  number (many aren't in an "IMO" category), and ships that found nothing are tried again.
- When there's no free photo, the other-ship popup says so with a link to the ship's
  VesselFinder page (their photos are theirs and can't be copied into the app), or says
  the lookup failed and why. Lookup failures are also written to the log.

## 0.36.2

- The "Other ships" switch (every ship on AIS) is now also at the end of the Ships tab,
  under All ships, not only in Layers › Traffic. Its count updates while the menu is open.

## 0.36.1

- Passage replays from Stats show the whole trip the passage was part of, from leaving
  port to the next port (Mariehamn or Helsinki for the Stockholm ferries, at most 18
  hours either side), not just the hour before and after. The map still opens on the
  stretch around the passage; zoom out for the rest, and Play runs the whole trip.

## 0.36.0

- Weather panel in the style of Windy: one colour layer at a time from a row of chips,
  with a colour legend on the map, a darker time bar with the days marked, and **tap
  the map for the value there**. New colour layers: Temperature, Wind gusts, Clouds
  (low, middle, high on tap), Rain and snow, Fog and visibility, Waves (with moving
  swell lines), Sea temperature, Sea level, Northern lights and Light pollution. Wind
  is now a colour layer too, with its moving lines on top. Pressure lines (isobars
  with highs and lows) and lightning go on top of any of them.
- The forecast layers share one run of FMI's HARMONIE model over the whole Baltic
  (Open-Meteo if FMI is down), stored an hour per file so memory stays low; waves, sea
  temperature and sea level come from Open-Meteo's marine forecast; lightning from FMI
  (last hour); northern lights from NOAA's aurora nowcast with the dark and the clouds
  taken into account, and tonight's Kp forecast in words.
- Light pollution from the Light Pollution Atlas 2025, with the zone and sky brightness
  (mag/arcsec²) wherever you tap. Downloaded once (about 1 MB), kept as a 9 MB file on the NAS.
- Sunset & sunrise: how colourful each of the next two days' sunsets and sunrises is
  likely to be, from the clouds overhead and towards the sun. Zoomed in, the best spots
  on land with an open view that way (from the terrain's height and what's around).
- Night sky: a star map for where your phone was last seen or the map's centre, with
  the planets, the Moon's phase and meteor showers, played through the night or a night
  forward and back; a list of what's worth seeing tonight; and a small note on the map
  (not on the phone) when there's something special and the clouds allow.

## 0.35.0

- Sharper Satellite view: from zoom 11 in, Finland (incl. Åland) shows the Land Survey's
  own aerial photos (0.5 m per pixel, taken in summer, renewed every three years), and
  Estonia shows Maa-amet's. Elsewhere, and where those have no picture (open sea), Esri's
  imagery shows as before. The NAS fetches the photos and keeps a copy (up to 400 MB,
  left out of backups). The Finnish photos come through Kapsi's free service with no key;
  if Kapsi is down, the Land Survey's own tiles are used with your API key (those stop at
  zoom 16). Settings › Data sources lists a photo service only while it's failing.

## 0.34.0

- Route tool: drag the automatic route onto another fairway and it goes that way. While
  you drag a dot on the line, the fairways deep enough for your draught glow faintly and
  the dot snaps onto one when it comes near (a ring shows where). Let go there and the
  route is planned again through that point, along that fairway; the point stays as a
  small diamond you can drag on or remove. Let go anywhere else and it's a bend off the
  fairways, as before. Works with a finger and with a mouse (hold Shift or Alt for a bend
  right next to a fairway). Snapping works from zoom 11.
- Fairway legs from a bend or a via point keep to the fairway they start on, instead of
  cutting straight across to another one nearby.
- Saved routes keep their via points.

## 0.33.0

- Other ships now show zoomed out too, down to all of Finland (was only from zoom 8,
  within 60 km). Wide views share one snapshot of every ship Digitraffic has, kept 30 s;
  marks get smaller further out. Costs a few MB on the server only while it's switched on.
- Photos for many more ships: besides Wikipedia, photos are looked up on Wikimedia Commons
  and Wikidata by the ship's IMO number, so the exact ship (fleet ships, cruise ships,
  cargo ships, tugs...). Tapping another ship shows its photo too when there is one; those
  are kept for the newest 300 ships and left out of backups. Ships that found nothing
  before are tried again once.
- The ship card and the other-ship popup link to VesselFinder and MarineTraffic.

## 0.32.1

- The alert areas (the inner-route lines and your phone's area) are no longer drawn on
  the map all the time. They show while you edit them, or when you ask to check them:
  Settings › Alerts › Show on map, or tap an area's name there to go to it. A small
  "Alert areas shown" pill under the conditions hides them again.

## 0.32.0

More in Stats, under the ship list (for the ships and direction chosen above):

- Highlights: days since the last inner passage (which ship, which way), the longest wait
  for one so far (with a bar showing how far the current wait has got), the busiest day
  for the inner route, and the fastest passage past Porkkala against the usual speed.
- In wind like now: how often ships took the inner route in wind like the wind at
  Mäkiluoto right now (same direction give or take 35°, within 3 m/s), against how often
  they did overall. It's what happened before, not a forecast, and it says so when there
  are too few passages to tell.
- Times past Porkkala: for each ship and direction, a strip with a dot for every passage
  in the last 60 days, the inner ones ringed in orange, the usual time marked and the
  latest one big, plus its usual speed and how late or early the latest was.
- The year at a glance: a square for every day, purple for outer route only, orange
  (darker for more) for days with inner passages. Tap a day to see its passages. On a
  phone it scrolls sideways and starts at the latest weeks.
- Passages now carry their speed in the stats data.

## 0.31.4

- Home Assistant backups are much smaller: they now leave out the drawn chart, the map
  tile caches and the weather overlays (together 1 to 2 GB, all downloaded again by
  themselves). Ship tracks, passages and stats, areas, settings, ship photos and the
  Land Survey key are still in every backup. After restoring a backup the drawn chart
  fills in again over about a day, zoomed-out first.

## 0.31.3

- Fixes the grid on the zoomed-out drawn chart, mostly on a computer screen: the thin
  line that hides the seams between neighbouring depth areas was only a third of a
  pixel there, so in the archipelago's thousands of small areas the seams let a grey
  speckle through, and the chart blocks' edges (where the pieces overlap instead) stood
  out as a light grid. That line is now always at least one screen pixel wide.

## 0.31.2

- Fixes the drawn chart showing tiles the phone kept from before an update (so 0.31.1's
  extra depth contours didn't show): in the first moments after the panel opens, before
  it knows the chart's version, tiles are now always fetched fresh, and nothing kept
  from then is used again.
- Note: Traficom's open data has extra depth contours only at 15 m and 30 m, so those
  are the faint lines you'll see. The official chart's many other contours come from
  Traficom's electronic chart cells, which aren't open data.

## 0.31.1

- More depth contours on the drawn chart up close: from zoom 14, the depth contours the
  paper chart leaves out (Traficom's data has many more than 3, 6, 10 and 20 m) are
  drawn as faint thin lines under the usual ones, as on the official chart. Switch them
  off with **More depth contours** under Layers → On the chart.

## 0.31.0

- The drawn chart is drawn in the background on the phone's spare cores (several tiles
  at once), so pinching and panning stay smooth while new tiles are drawn. Falls back to
  drawing on the page where the browser can't.
- Tiles are kept on the phone (up to about 150 MB of its storage): an area you've looked
  at before opens straight from the phone, even after closing the app, without asking
  Home Assistant. A tile older than a day still shows at once and is checked behind it.
- While pinching, the next zoom's tiles are asked for as soon as it's reached, not after
  your fingers lift; tiles show at once instead of fading in; two zooms out are fetched
  ahead too, and up to 24 MB of tiles kept in memory (was 12).

## 0.30.0

- Snappier drawn chart when zooming and panning:
  - While new tiles load, the chart from before the zoom stays on screen (stretched)
    instead of going blank; a tile whose area is still downloading is asked for again
    after 0.7 s (was 2.5 s).
  - After each move, the tiles round the view, the view one zoom out and the middle one
    zoom in are fetched ahead (their data only, at most about 12 MB kept in the browser),
    so the next pan or zoom draws at once instead of asking Home Assistant again.
  - On phones, tiles are now asked for while your finger is still panning, not only after
    it lifts.
  - On the NAS, tiles already made are read on a thread of their own, a tile that only
    needs cutting from data on disk is waited for instead of answered "not yet", and the
    background chart downloads pause while you're using the map.
- Fixes town and sea names in the wrong place on the zoomed-out drawn chart (Kotka, Salo,
  Hamina, Kokkola, Tornio, Pohjanlahti... all over the lakes): villages, houses and lake
  bays with those names were taken for the towns and seas. Those names now show zoomed
  out only where the real town or sea is.

## 0.29.0

- Route tool: easier to leave the fairway where you want. Pulling the route no longer
  plans it again round the fairways: drag any dot on the line and only that bit moves.
  - Each fairway turn is a filled dot to drag; the hollow dot halfway along each piece
    pulls out a new bend.
  - **Straight from here to…** (tap a stop, a dot or the line): then tap where to rejoin
    the route, and the stretch between becomes one straight line, cutting out a loop
    round an island.
  - Tap a leg for **Along the fairways** to plan it along the fairways again, or
    **Straight line**.
- The panel lists legs between your own numbered stops (fairway, straight or own line);
  bends aren't numbered. Saved routes keep their bends.
- A fairway leg whose chart areas are still downloading is planned again by itself a
  little later.

## 0.28.0

- Route tool: a **Straight / Along fairways** switch at the top of the panel replaces the
  Auto button. With Along fairways, tap the start and the end and the leg is planned
  along the marked fairways right away; each further tap adds another fairway leg.
- Easy to change part of the route: drag the dot halfway along a leg to pull the route
  through a new point there, or tap a leg (on the map or in the panel) to make it
  straight, follow the fairways again, add a point, or turn its turns into points you can
  move. Only the legs you touch are planned again, so stretches you moved off the
  fairways stay put.
- Find a harbour by name (magnifier button) to start or end the route there.
- Saved routes keep which legs follow the fairways, so Edit opens them as drawn. Routes
  can have up to 500 points (was 200).
- Tapping a route point now opens a small menu with Remove, instead of removing it
  straight away.

## 0.27.2

- Lit buoys, spar buoys and other lit marks now show on the drawn chart from zoom 10
  (they waited for zoom 12), so a lone lit buoy out at sea is there when you zoom out, as
  on the official chart. Lighthouses and sector lights still show furthest out; unlit
  marks from zoom 13.

## 0.27.1

- Fixes the zoomed-out drawn chart looking griddy: a fine grey grid showed all over the
  shallows, from thin seams where neighbouring depth areas meet along straight edges.
  Those edges now get the same seam cover as the others; only the edges where an area
  was cut to its chart block are left alone, and there the next block's area overlaps.
- At zoom 10 the depth contours are thinned out as further out (no rings round single
  shoals), and a little more at zoom 9.
- The base map's sea is drawn from its own shapes, so no thin line can show across the
  land where its pieces meet.

## 0.27.0

- The drawn chart now covers all of Finland and is drawn at every zoom, down to the
  whole country on one screen. The scanned overview chart no longer shows under it when
  zoomed out.
- Zoomed out, the chart looks like a small-scale chart: fewer depth contours (no rings
  round every single shoal), smoothed, deeper ones only as you zoom further out, and only
  the major lights and the seas' names.
- Where the chart has no data yet, or none at all (inland), the land, islands and lakes
  are drawn from Natural Earth's world map. So there are no tan squares round the areas
  already downloaded, no other map showing through, and the lakes are blue.
- **Keep chart on the NAS** now downloads all Finnish waters: the Gulf of Finland first
  (what was kept before), then the Archipelago Sea and Åland, the Bothnian Sea, the Bay of
  Bothnia and the charted lakes, 2,645 areas in all. It first fetches what the zoomed-out
  chart needs everywhere, so the whole country fills in within hours, then the detail. The
  first full download takes a day or so; the chart works meanwhile.
- Zooming out stays quick: the zoomed-out tiles are made in the background as areas come
  in and kept on the NAS (about 10 MB for all of Finland), and sent as they are. More
  disk for drawn tiles you've viewed (512 MB, was 64 MB); RAM use is unchanged.
- Aid-to-navigation faults, the wind forecast and rescue aircraft now cover the whole
  coast too, not just the Gulf of Finland.

## 0.26.8

- Fixes the made-up islands still left out at sea on the drawn chart (off Bogskär, south
  of Åland). Out there Traficom's data sometimes has a deep area with a hole for a shoal
  but not the shoal's own, shallower area, and no depth contours either, so nothing
  covered the hole and the chart drew it as an island. Now a place is only land when the
  water round it starts at the surface, as it does along every real shore. The rest is
  drawn as deep water; the soundings there still give the depth.

## 0.26.7

- Fixes made-up land out at sea on the drawn chart (the tan strips and patches south of
  Ekenäs). Traficom's depth areas stop short of the Finnish sea border in places, and
  everything they didn't cover was painted as land. Now only real land is: islands, the
  shore along the shallow-water band, and the mainland (checked against Natural Earth's
  world map). Where the depths stop, the sea is drawn as plain deep water, without the
  black coastline that used to run along the edge.
- Areas out at sea where Traficom has no depths at all are drawn as deep water too, where
  they used to stay empty (showing the background map through the chart).
- Fixes areas that never got a chart at all (empty squares, many in the Archipelago Sea):
  a depth area too big to download in one go was meant to be read in parts since 0.18.1,
  but a mix-up between two settings of the same name made the app refuse every such
  area. Those areas download now; each is a big download, so they take a while the
  first time.

## 0.26.6

- Fixes the drawn chart staying blank after the 0.26.4 update. Each area waited for its new
  OpenStreetMap layers (cables, nature reserves) and the new Traficom rocks before it was
  drawn, and OpenStreetMap's server was full. Now only the depths are ever waited for:
  everything else is drawn from what's already on the NAS while newer data downloads, and
  layers that haven't come yet are added when they do (the panel asks for those tiles
  again after 10 s, 30 s, 1.5 min and 5 min).
- A wait for OpenStreetMap no longer holds up the other chart downloads, and when its
  server says it's full the app leaves it alone for a while.
- An OpenStreetMap failure no longer shows up as Traficom failing under Data sources.

## 0.26.5

- Submarine cables are drawn as on the paper chart (Sailmate): a thinner, pinker magenta
  line with longer, smoother waves, each a little longer than a depth figure is high.
- A cable no longer breaks or jumps where it crosses from one map tile to the next.

## 0.26.4

- Boat routes are a thin solid line, as on the paper chart (they were dashed).
- The number along a fairway is its navigable depth where Väylä gives one ("(1,8)" as on
  the paper chart), otherwise the design draught as before. The fairway data is fetched
  again once for this.
- Submarine cables and pipelines from OpenStreetMap, while Traficom hasn't published its
  own. When Traficom does, the app switches to theirs by itself.
- Nature reserves' borders as a green dashed line, from OpenStreetMap.
- Both come through the same OpenStreetMap service as the Swedish and Estonian side, a
  request per area the first time it's looked at, kept for a month.

## 0.26.3

- Masts and chimneys show their height as the paper chart does: the top above the sea.
  The Land Survey gives the structure's own height in millimetres (Husö's mast came out as
  61000); it's now metres, plus the ground under it from the nearest land contour, so
  Husö reads about (84) like the paper chart. Good to within a contour step (5 m or less).
- Rocks from Traficom's own chart data are added to the Land Survey's, where Traficom
  publishes them. The Land Survey misses some rocks the paper chart has, mostly ones found
  by depth surveys out in open water. A rock both have is drawn once.

## 0.26.2

- Rocks no longer pile up when zoomed out (up to zoom 14): rocks close together are drawn
  as one cluster sign, standing on one of them and showing a rock above water if there is
  one. No place with rocks is left without a sign; small rock clusters that used to vanish
  when zooming out now keep theirs. From zoom 15 every rock is drawn.
- All light symbols are see-through: flares, sector arcs, rings and wedges, so lines,
  marks and depths under them show.

## 0.26.1

- Sector wedges are see-through yellow, so the lines, depths and contours under them show,
  as on the paper chart.
- A wedge's edges are thin dotted lines that go on about a third past the yellow, with
  its W at the yellow's end.
- Wedges no longer overlap: of two that would meet, the one meeting the other nearer its
  own end stops short of it. Dotted edges stop short of another wedge too.

## 0.26.0

The rest of the UI review (items 19 to 30).

- Ships tab: tap one of your ships to find it on the map. Each has a bell to turn its
  alerts on or off, and Remove sits behind ⋯ so it isn't tapped by mistake. Search results
  say what Follow (map only) and Add (alerts and stats) mean. Fleets moved here from
  Layers, and each folds open to list its ships.
- Layers: one line per layer with its switch; the explanation is behind ⓘ. A map key at
  the bottom explains the lines, dots and colours on the map.
- Settings:
  - Alerts are in one box: where they go, whether only while your phone is in your area,
    the last alert sent, your areas with Edit, and a bell per ship. A link opens the
    app's configuration in Home Assistant.
  - A Data sources list shows each source with a green, red or grey dot: ship positions,
    Home Assistant, FMI, the weather overlays, aircraft and each chart provider.
  - Memory is a bar by part with the peak marked; tap it for the numbers.
  - Downloading the chart for the NAS shows a progress bar.
- Dates are written the same way everywhere, 24-hour: "Thu 30 Oct, 07:34".
- The ship card has a bell for that ship's alerts and a Stats link that opens Stats for
  that ship alone.
- On a computer: Esc closes the side panel or the card, and the left and right arrow
  keys step through the Stats bars, or the days once a day is picked.

## 0.25.1

- Sector lights' narrow white sectors (the yellow wedges) are now all about one length,
  about 1 km, as on the paper chart. A wedge is shorter only where its fairway turns off
  sooner. They no longer try to follow the fairway's width.
- Each wedge has a thin dark edge and its W at the far end, as on the paper chart, instead
  of a W at the arc.

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
