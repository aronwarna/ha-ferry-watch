"""SMHI's daily ice chart (icemap.py): reading the shapefile, a tap, drawing, the store."""

import asyncio
import io
import time

from PIL import Image

from ferrywatch import icechart as ic
from ferrywatch import icemap as im
from ferrywatch.overlays import Overlays, parse_instant
from ferrywatch.simoverlays import fake_icemap_get, shapefile_zip

DAY = parse_instant("2026-02-25")
# A square of heavily ridged 9+/10 ice in the Gulf of Bothnia with a hole of open water,
# and grey fast ice by the coast.
SQUARE = [[20.0, 63.0], [22.0, 63.0], [22.0, 64.0], [20.0, 64.0], [20.0, 63.0]]
HOLE = [[20.8, 63.4], [21.2, 63.4], [21.2, 63.6], [20.8, 63.6], [20.8, 63.4]]
FAST = [[22.0, 63.0], [23.0, 63.0], [23.0, 64.0], [22.0, 64.0], [22.0, 63.0]]
AREAS = [[91, 5, 4, 20, 30, 45, [SQUARE, HOLE]], [92, 8, 0, 35, 50, 80, [FAST]], [98, 1, 0, 0, 0, 0, [HOLE]]]


def _day():
    return im.Day(im.parse_zip(shapefile_zip(AREAS, "2026-02-25")))


def test_parse_and_tap():
    data = im.parse_zip(shapefile_zip(AREAS, "2026-02-25"))
    assert len(data["areas"]) == 2  # ice free (98) left out
    d = im.Day(data)
    a = d.at(63.8, 20.5)
    assert a and a["c"] == 91 and a["d"] == 4
    assert d.at(63.5, 21.0) is None  # in the hole
    t = im.describe(a, "2026-02-25")
    assert t["class"] == "Very close drift ice" and t["short"] == "9+/10" and t["deform"] == "Heavily ridged ice"
    assert (t["min"], t["max"]) == (20, 45)
    fast = im.describe(d.at(63.5, 22.5), "2026-02-25")
    assert fast["class"] == "Fast ice"
    assert d.thickest() == 80


def test_colours():
    assert im.colour_of(91, 5) == im.COLOURS[-1][1]
    assert im.colour_of(92, im.FAST) == im.FAST_COLOUR
    assert im.colour_of(10, 2) == im.COLOURS[1][1]


def test_draw_tile():
    d = _day()
    z = 7
    wx, wy = im._world(20.5, 63.8, z)
    x, y = int(wx // 256), int(wy // 256)
    img = Image.open(io.BytesIO(im.draw_tile(d, z, x, y))).convert("RGBA")
    px = img.getpixel((int((wx - x * 256) * 2), int((wy - y * 256) * 2)))
    assert px[3] > 200 and px[0] > 150  # red-ish 9+/10
    hx, hy = im._world(21.0, 63.5, z)
    if int(hx // 256) == x and int(hy // 256) == y:
        assert img.getpixel((int((hx - x * 256) * 2), int((hy - y * 256) * 2)))[3] == 0  # the hole
    # Far from any ice: nothing.
    assert im.draw_tile(d, 7, 0, 0) is None


def test_labels_do_not_pile_up():
    d = _day()
    z = 8
    wx, wy = im._world(21.0, 63.5, z)
    labels = im.labels_near(d, z, int(wx // 256), int(wy // 256))
    for i, (ax, ay, _) in enumerate(labels):
        for bx, by, _ in labels[i + 1:]:
            assert abs(ax - bx) >= im.LABEL_BOX[0] or abs(ay - by) >= im.LABEL_BOX[1]
    assert any(text == "20-45" for *_, text in labels)


def test_store_and_overlay(tmp_path):
    async def run():
        maps = im.IceMaps(tmp_path / "maps", None, fake_icemap_get)
        now = time.time()
        start, _ = ic.last_season(now)
        days = await maps.days(start, now)
        assert days and all(time.gmtime(d).tm_mon in (11, 12, 1, 2, 3, 4, 5) for d in days)
        got = await maps.ready(DAY)
        assert got is not None and got.has_ice and maps.kept(DAY)
        assert maps.summary(DAY)[0] > 0
        # Read from disk the second time.
        again = im.IceMaps(tmp_path / "maps", None, None)
        assert (await again.ready(DAY)) is not None

        reg = Overlays(tmp_path / "ov", None)
        chart = ic.register(reg, tmp_path / "ic", None, None, None, maps)
        await chart.times(None, DAY + 86400)
        body, final = await chart.tile(DAY, 7, 72, 32)
        assert body and final
        v = await chart.value(65.0, 24.5, DAY)
        assert v and v["src"] == "chart"
        assert str(DAY) in chart.info(DAY + 86400)["read"]
    asyncio.run(run())
