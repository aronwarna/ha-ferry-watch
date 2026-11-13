"""Automatic route planning over open water (smartroute.py): the grid from chart layers,
the motor boat's way, and weather routing for the sailing boat."""

import asyncio
import time

from PIL import Image

from ferrywatch import dayscore
from ferrywatch import smartroute as S
from ferrywatch.geo import distance_m
from ferrywatch.simulate import fake_chart_layer
from ferrywatch.vchart import VectorChart, build_block, lonlat_to_mercator


def test_depth_class_by_what_the_boat_needs():
    assert S.depth_class(3, 6, 1.1) == 2  # deep enough throughout
    assert S.depth_class(0, 3, 1.1) == 1  # may be: the small boat needs under half way into the band
    assert S.depth_class(0, 3, 2.5) == 0  # the sailing boat doesn't go there
    assert S.depth_class(3, 6, 2.5) == 2
    assert S.depth_class(-1, 0, 0.5) == 0  # drying
    assert S.depth_class(None, None, 2.5) == 2  # no values: deep, as the chart draws it


def square(x0, y0, x1, y1):
    return [[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]


def open_grid(w=80, h=60, walls=()):
    """A grid of open water with walls (x0, y0, x1, y1 in cells) of land."""
    grid = S.Grid(1164, 594, 1164, 594, 40.0)
    grid.w, grid.h = w + 2 * S.BORDER, h + 2 * S.BORDER
    cls = Image.new("L", (grid.w, grid.h), 2)
    for x0, y0, x1, y1 in walls:
        cls.paste(0, (x0 + S.BORDER, y0 + S.BORDER, x1 + S.BORDER, y1 + S.BORDER))
    S.finish(grid, cls, Image.new("L", (grid.w, grid.h), 0))
    return grid


def test_block_cells_from_chart_layers():
    from ferrywatch.vchart import block_bounds
    bx, by = 1164, 594
    minx, miny, maxx, maxy = block_bounds(bx, by)
    w, h = maxx - minx, maxy - miny
    at = lambda fx, fy: [minx + w * fx, maxy - h * fy]  # noqa: E731
    area = lambda x0, y0, x1, y1, hole=None, **p: {  # noqa: E731
        "type": "Feature", "properties": p,
        "geometry": {"type": "Polygon", "coordinates": [[at(*q) for q in square(x0, y0, x1, y1)]]
                     + ([[at(*q) for q in square(*hole)]] if hole else [])}}
    layers = build_block(bx, by, {
        # Deep water left with a shoal in a hole, the 0-3 m band right, land (nothing) at the bottom.
        "depth": [area(0, 0, 0.5, 0.8, hole=(0.2, 0.2, 0.3, 0.3), drval1=6, drval2=10),
                  area(0.2, 0.2, 0.3, 0.3, drval1=0, drval2=3),
                  area(0.5, 0, 1, 0.8, drval1=0, drval2=3)],
        "sounding": [{"type": "Feature", "properties": {"depth": 0.4},
                      "geometry": {"type": "Point", "coordinates": at(0.75, 0.4)}}],
        "fline": [{"type": "Feature", "properties": {"mitoitussyvays": 2.0, "jnro": 1},
                   "geometry": {"type": "LineString", "coordinates": [at(0.05, 0.9), at(0.95, 0.9)]}}],
    })
    cls, fw, lines = S.render_block(layers, 100, 100, 1.1, 0.6, 100.0)
    px = cls.load()
    assert px[10, 10] == 2 and px[25, 25] == 1 and px[60, 20] == 1 and px[50, 95] == 0
    assert px[75, 40] == 0  # the shallow sounding closes its cell
    assert 128 in (fw.load()[50, 89], fw.load()[50, 90])  # 128: a fairway, 255: a green small-craft route
    # The sailing boat: the 0-3 m band is closed, and the 2.0 m fairway is too shallow.
    cls, fw, lines = S.render_block(layers, 100, 100, 2.5, 1.9, 100.0)
    assert cls.load()[60, 20] == 0 and cls.load()[10, 10] == 2 and not fw.getbbox()


def test_power_goes_round_land_and_straightens():
    grid = open_grid(walls=[(38, 0, 42, 45)])  # a wall from the top, open below
    found = S.search_power(grid, (10 + S.BORDER, 5 + S.BORDER), (10 + S.BORDER, 75 + S.BORDER), 20)
    assert found
    rows = [r for r, _ in found["cells"]]
    assert max(rows) >= 45 + S.BORDER  # round the wall's end
    idx = S.SmartRouter._shape_power(grid, found, 20)
    assert 3 <= len(idx) <= 12  # a few straight stretches, not a staircase
    cells = [found["cells"][i] for i in idx]
    assert all(S._open_line(grid, a, b) for a, b in zip(cells, cells[1:]))
    # Nothing through a wall right across.
    closed = open_grid(walls=[(38, 0, 42, 60)])
    assert S.search_power(closed, (10 + S.BORDER, 5 + S.BORDER), (10 + S.BORDER, 75 + S.BORDER), 20) is None


def test_speed_limit_area_is_gone_round_when_that_is_quicker():
    grid = open_grid(w=120, h=60)
    lat0, lon0 = grid.latlon(20 + S.BORDER, 40 + S.BORDER)
    lat1, lon1 = grid.latlon(40 + S.BORDER, 80 + S.BORDER)
    area = {"kmh": 5, "ring": [[lat0, lon0], [lat0, lon1], [lat1, lon1], [lat1, lon0]]}
    cls = Image.new("L", (grid.w, grid.h), 2)
    S.finish(grid, cls, Image.new("L", (grid.w, grid.h), 0), [area])
    a, b = (30 + S.BORDER, 5 + S.BORDER), (30 + S.BORDER, 115 + S.BORDER)
    found = S.search_power(grid, a, b, 25)
    assert not any(grid.limit[r * grid.w + c] for r, c in found["cells"])
    slow = S.search_power(grid, a, b, 2)  # slower than the limit: straight through
    assert any(grid.limit[r * grid.w + c] for r, c in slow["cells"])


def test_nearest_open_and_snap():
    grid = open_grid(walls=[(0, 0, 20, 60)])
    assert S.nearest_open(grid, 30 + S.BORDER, 5 + S.BORDER)[1] >= 20 + S.BORDER


class FakeWeather:
    """Wind from one direction everywhere, turning `shift` degrees an hour."""

    def __init__(self, direction, ms, shift=0, fail=False):
        self.direction, self.ms, self.shift, self.fail = direction, ms, shift, fail

    async def conditions(self, pts, now=None):
        if self.fail:
            return None, None, {"fmi": "offline"}
        hrs = dayscore.hours(time.time())
        air = dayscore.Series(hrs, dayscore.AIR, len(pts))
        sea = dayscore.Series(hrs, dayscore.SEA, len(pts))
        for i in range(len(pts)):
            for j, t in enumerate(hrs):
                air.set(i, t, "ms", self.ms)
                air.set(i, t, "dir", (self.direction + self.shift * j) % 360)
                sea.set(i, t, "wave", 0.4)
        air.extra["air"] = "fake"
        return air, sea, {}


def sail(grid, a, b, direction, ms, comfortable=False):
    pts, shape = S.wind_points(grid)
    air, sea, _ = asyncio.run(FakeWeather(direction, ms).conditions(pts))
    return S.search_sail(grid, a, b, S.Wind(grid, pts, air, sea, shape), time.time(), comfortable)


def test_sailing_depends_on_the_wind():
    grid = open_grid(w=150, h=150)
    a, b = (75 + S.BORDER, 10 + S.BORDER), (75 + S.BORDER, 140 + S.BORDER)  # due east, 5.2 km
    reach = sail(grid, a, b, 180, 7)  # wind on the beam
    beat = sail(grid, a, b, 90, 7)  # dead against
    calm = sail(grid, a, b, 90, 1)
    assert set(reach["how"][1:]) == {"s"}
    assert beat["seconds"] > reach["seconds"] * 1.3
    assert "m" not in beat["how"][1:]  # it sails: tacks drawn out, or beating made good
    assert set(calm["how"][1:]) == {"m"}
    assert abs(calm["seconds"] - 5200 / (5.5 * S.KN)) < 120
    # Comfortable in a head wind: no beating made good straight into it if a tack is as easy.
    comfy = sail(grid, a, b, 90, 7, comfortable=True)
    assert comfy["seconds"] >= beat["seconds"] - 1


def test_no_motoring_along_the_shore_to_dodge_a_beat():
    # A shore along the bottom: its narrow water lets the engine run, but that must not
    # count as quicker than sailing out in the open.
    grid = open_grid(w=150, h=80, walls=[(0, 70, 150, 80)])
    a, b = (40 + S.BORDER, 10 + S.BORDER), (40 + S.BORDER, 140 + S.BORDER)
    beat = sail(grid, a, b, 90, 8)
    assert beat["how"][1:].count("m") < len(beat["how"]) * 0.1


def test_router_plans_on_the_simulated_chart(tmp_path):
    async def run():
        chart = VectorChart(tmp_path, None, fake_chart_layer)
        start, end = (59.972, 24.60), (59.946, 24.40)
        router = S.SmartRouter(chart, FakeWeather(200, 8))
        first = await router.route(start, end, "buster_xl")
        assert first["downloading"] > 0
        await router._downloading
        out = await router.route(start, end, "buster_xl")
        assert out["downloading"] == 0 and "error" not in out, out
        assert out["points"][0] == [59.972, 24.6] and out["points"][-1] == [59.946, 24.4]
        assert len(out["hows"]) == len(out["points"]) - 1
        length = sum(distance_m(*a, *b) for a, b in zip(out["points"], out["points"][1:]))
        assert abs(length - out["metres"]) < 5 and length < 1.4 * distance_m(*start, *end)
        assert abs(out["seconds"] - length / (26 * S.KN)) < 0.2 * out["seconds"]
        # The sailing boat: two routes from the forecast, the time from the wind.
        boat = await router.route(start, end, "sail35", style="comfort")
        assert "error" not in boat, boat
        assert boat["style"] == "comfort" and boat["need"] == 2.5 and boat["seconds"] > out["seconds"] * 2
        assert boat["same"] or boat["alt"]["style"] == "fast"
        assert set(boat["hows"].lower()) <= set("sbgm")
        # Without a forecast: the way under engine, and it says so.
        offline = await S.SmartRouter(chart, FakeWeather(0, 0, fail=True)).route(start, end, "sail35")
        assert offline["style"] == "motor" and "engine" in offline["warnings"][0]
        # A start far inland has no water to join.
        assert "error" in await router.route((61.5, 24.6), end, "buster_xl")

    asyncio.run(run())


def test_corridor_and_cell_size_grow_with_the_trip():
    near = S.corridor((59.97, 24.6), (59.95, 24.4))
    far = S.corridor((59.8, 22.9), (60.4, 26.9))  # Hanko to Kotka, about 125 NM
    assert S.cell_size(near, 60, S.MAX_CELLS) == 40
    size = S.cell_size(far, 60, S.MAX_CELLS)
    assert size and 80 <= size <= 400
    count = (far[2] - far[0] + 1) * (far[3] - far[1] + 1)
    assert count <= S.MAX_BLOCKS
    assert S.cell_size((0, 0, 200, 200), 60, S.MAX_CELLS) is None
    x, y = lonlat_to_mercator(24.6, 59.97)
    assert x > 0 and y > 0


# --- shelter, fairways and smooth lines (0.54) -------------------------------------------------

def island_coast(w=300, h=160):
    """Open sea in the south, a row of islands half way up, a sheltered channel behind them."""
    grid = S.Grid(1164, 594, 1164, 594, 80.0)
    grid.w, grid.h = w + 2 * S.BORDER, h + 2 * S.BORDER
    cls = Image.new("L", (grid.w, grid.h), 2)
    cls.paste(0, (0, 0, grid.w, 20))  # the mainland
    for x in range(10, w, 26):  # islands with gaps between them
        cls.paste(0, (x, 45, x + 20, 60))
    S.finish(grid, cls, Image.new("L", (grid.w, grid.h), 0))
    return grid


def test_small_boat_keeps_behind_the_islands_in_wind():
    grid = island_coast()
    a, b = (64 + S.BORDER, 5 + S.BORDER), (64 + S.BORDER, 295 + S.BORDER)  # both just outside the islands
    plain = S.search_power(grid, a, b, 20)
    assert min(r for r, _ in plain["cells"]) >= 60 + S.BORDER  # the straight way, outside
    expo = S.exposure(grid, 180)  # wind from the south, off the open sea
    assert expo[(120 + S.BORDER) * grid.w + 150] > 240  # open sea
    assert expo[(35 + S.BORDER) * grid.w + 20 + S.BORDER] < 60  # behind an island
    found = S.search_power(grid, a, b, 20, expo, 1.2)
    inside = sum(1 for r, _ in found["cells"] if r < 45 + S.BORDER) / len(found["cells"])
    assert inside > 0.7
    idx = S.SmartRouter._shape_power(grid, found, 20)
    out = S.describe(grid, found["cells"], idx, found, False, grid.latlon(*a), grid.latlon(*b))
    assert out["exposed_nm"] < 0.25 * out["metres"] / 1852
    # A northerly: the islands' south side is the lee, the channel's north shore still shelters.
    assert S.exposure(grid, 0)[(64 + S.BORDER) * grid.w + 20 + S.BORDER] < expo[(64 + S.BORDER) * grid.w + 20 + S.BORDER]


def test_route_stays_on_a_fairway_and_on_its_line():
    grid = S.Grid(1164, 594, 1164, 594, 80.0)
    grid.w, grid.h = 240 + 2 * S.BORDER, 120 + 2 * S.BORDER
    cls = Image.new("L", (grid.w, grid.h), 2)
    fw = Image.new("L", (grid.w, grid.h), 0)
    line = [(10.3, 60.4), (100.6, 60.4), (130.2, 40.7), (230.5, 40.7)]  # x, y: a fairway with a dog-leg
    from PIL import ImageDraw
    ImageDraw.Draw(fw).line(line, fill=255, width=1)
    S.add_fairway_lines(grid, [line], 0, 0)
    S.finish(grid, cls, fw)
    a, b = (60, 10), (40, 230)
    found = S.search_power(grid, a, b, 20)
    on = sum(1 for r, c in found["cells"] if grid.flags[r * grid.w + c] & S.FAIRWAY) / len(found["cells"])
    assert on > 0.9  # not a line of its own beside the fairway or across its bend
    idx = S.SmartRouter._shape_power(grid, found, 20)
    assert len(idx) <= 6
    out = S.describe(grid, found["cells"], idx, found, False, grid.latlon(*a), grid.latlon(*b))
    assert out["nm"].get("fairway", 0) > 0.9 * out["metres"] / 1852
    # The turning points lie on the fairway's own line (its bends), not in the middle of cells.
    bends = [grid.latlon(y - 0.5, x - 0.5) for x, y in line[1:3]]
    for p in out["points"][1:-1]:
        assert min(distance_m(*p, *q) for q in bends) < 60


def test_no_small_kinks_in_open_water():
    grid = open_grid(w=200, h=80)
    found = S.search_power(grid, (10 + S.BORDER, 5 + S.BORDER), (60 + S.BORDER, 195 + S.BORDER), 20)
    assert len(S.SmartRouter._shape_power(grid, found, 20)) == 2  # one straight line


def test_shelter_weight_follows_the_wind():
    grid = island_coast()
    router = S.SmartRouter(None, FakeWeather(200, 12))
    _, buster = S.boats.boat("buster_m")
    got = asyncio.run(router._shelter(grid, buster, None))
    assert got["wind_ms"] == 12 and got["wind_from"] == 200 and got["weight"] == round(0.7 * 2.0, 2)
    calm = asyncio.run(S.SmartRouter(None, FakeWeather(200, 1))._shelter(grid, buster, None))
    assert calm["weight"] == round(0.7 * 0.35, 2) and calm["wind_from"] is None
    assert asyncio.run(S.SmartRouter(None, FakeWeather(0, 5, fail=True))._shelter(grid, buster, None))["weight"] == 0.7
    assert asyncio.run(router._shelter(grid, S.boats.boat("sail35")[1], None))["weight"] == 0


def test_small_boat_takes_the_green_boat_route():
    from PIL import ImageDraw
    grid = S.Grid(1164, 594, 1164, 594, 80.0)
    grid.w, grid.h = 240 + 2 * S.BORDER, 120 + 2 * S.BORDER
    cls = Image.new("L", (grid.w, grid.h), 2)
    fw = Image.new("L", (grid.w, grid.h), 0)
    line = [(10, 60), (50, 10), (190, 10), (230, 60)]  # a small-craft route a fifth longer than straight
    ImageDraw.Draw(fw).line(line, fill=255, width=1)
    S.finish(grid, cls, fw)
    a, b = (60, 10), (60, 230)
    on = lambda found: sum(1 for r, c in found["cells"] if grid.flags[r * grid.w + c] & S.TRUNK) / len(found["cells"])  # noqa: E731
    assert on(S.search_power(grid, a, b, 20)) < 0.2  # counted as any fairway: straight across
    found = S.search_power(grid, a, b, 20, fairway=tuple(S.boats.boat("buster_m")[1]["fairway"]))
    assert on(found) > 0.9
    idx = S.SmartRouter._shape_power(grid, found, 20)
    out = S.describe(grid, found["cells"], idx, found, False, grid.latlon(*a), grid.latlon(*b))
    assert out["trunk_nm"] > 0.9 * out["metres"] / 1852 and len(idx) <= 6


def test_places_to_check_and_room_from_shore():
    # A gap between two islands 5 cells (200 m) wide: the only way through.
    walls = [(38, 0, 42, 28), (38, 33, 42, 60)]
    grid = open_grid(walls=walls)
    a, b = (30 + S.BORDER, 5 + S.BORDER), (30 + S.BORDER, 75 + S.BORDER)
    found = S.search_power(grid, a, b, 20)
    idx = S.SmartRouter._shape_power(grid, found, 20)
    out = S.describe(grid, found["cells"], idx, found, False, grid.latlon(*a), grid.latlon(*b))
    near = [c for c in out["checks"] if c["kind"] == "near"]
    assert len(near) == 1 and near[0]["m"] >= 150  # the gap, said once, with where it is
    lat, lon = grid.latlon(30 + S.BORDER, 40 + S.BORDER)
    assert distance_m(near[0]["lat"], near[0]["lon"], lat, lon) < 150
    # In open water there is nothing to check.
    clear = open_grid()
    found = S.search_power(clear, a, b, 20)
    out = S.describe(clear, found["cells"], [0, len(found["cells"]) - 1], found, False, clear.latlon(*a), clear.latlon(*b))
    assert out["checks"] == []
    # Wide room closes more water round the shore than close room.
    cls = Image.new("L", (grid.w, grid.h), 2)
    for x0, y0, x1, y1 in walls:
        cls.paste(0, (x0 + S.BORDER, y0 + S.BORDER, x1 + S.BORDER, y1 + S.BORDER))
    counts = {}
    for name, room in S.SHORE_ROOM.items():
        g = open_grid()
        S.finish(g, cls, Image.new("L", (g.w, g.h), 0), None, room)
        counts[name] = (sum(1 for f in g.flags if f), sum(1 for f in g.flags if f & S.NEAR))
    assert counts["close"][0] >= counts["normal"][0] > counts["wide"][0]
    assert counts["close"][1] < counts["normal"][1] < counts["wide"][1]


def test_motor_boat_gets_the_direct_route_beside_the_sheltered_one(tmp_path):
    class Chart:  # no chart on disk: the grid is given
        pass
    router = S.SmartRouter(Chart(), FakeWeather(180, 12))
    grid = island_coast()

    async def fixed(*args, **kwargs):
        return grid
    router._grid = fixed
    a, b = grid.latlon(64 + S.BORDER, 5 + S.BORDER), grid.latlon(64 + S.BORDER, 295 + S.BORDER)
    out = asyncio.run(router.route(a, b, "buster_m"))
    assert out["style"] == "shelter" and out["alt"]["style"] == "direct"
    assert out["alt"]["metres"] < out["metres"] and out["alt"]["exposed_nm"] > out["exposed_nm"]
    other = asyncio.run(router.route(a, b, "buster_m", style="direct"))
    assert other["style"] == "direct" and other["alt"]["style"] == "shelter"
    assert other["points"] == out["alt"]["points"]
