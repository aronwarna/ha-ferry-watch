"""The FMI ice chart overlay: days, grey decoding, the day grid, drawing and a tap."""

import asyncio
import io
import json

from PIL import Image

from ferrywatch import icechart as ic
from ferrywatch.overlays import Overlays, parse_instant
from ferrywatch.simoverlays import _grey_of, fake_icechart_get, sim_ice

NOW = parse_instant("2026-11-09T12:00:00Z")
PEAK = parse_instant("2026-02-25")


def test_seasons():
    assert ic.season_start(NOW) == parse_instant("2026-11-01")
    assert ic.season_start(parse_instant("2026-03-01")) == parse_instant("2025-11-01")
    assert ic.season_start(parse_instant("2026-08-01")) == parse_instant("2025-11-01")
    assert ic.last_season(NOW) == (parse_instant("2025-11-01"), parse_instant("2026-06-01"))


def test_days_kept_are_this_and_last_winter():
    days = list(range(parse_instant("2024-12-01"), parse_instant("2026-11-09"), 86400))
    kept = ic.keep_days(days, NOW)
    assert kept[0] == parse_instant("2025-11-01")
    assert parse_instant("2026-07-01") not in kept
    assert kept[-1] == parse_instant("2026-11-08")
    assert ic.peak_day(kept, NOW) == PEAK
    assert ic.fallback_days(NOW)[-1] == parse_instant("2026-11-08")


def test_grey_models_and_choice():
    assert ic._lstar(0) == 0 and abs(ic._lstar(255) - 1) < 1e-6
    # Greys made by the simulator (even lightness) are read back within a percent.
    for v in (0, 7, 35, 64, 86, 100):
        g = _grey_of(v / 100)
        assert abs(ic.lut("lightness", 100)[g] - v) <= 1
    pairs = [(_grey_of(v / 100), float(v)) for v in (10, 30, 50, 70, 90)]
    assert ic.pick_model(pairs, 100) == "lightness"
    assert ic.pick_model([(g, g / 255 * 100) for g in (30, 100, 200)], 100) == "linear"
    assert ic.pick_model([], 100) == "lightness"


def test_parse_info():
    assert ic.parse_info(b'{"features":[{"properties":{"value":42.5}}]}') == 42.5
    assert ic.parse_info(b'{"features":[{"properties":{"ice_concentration":7}}]}') == 7.0
    assert ic.parse_info(b'{"features":[]}') is None
    assert ic.parse_info(b"oops") is None


def test_classes():
    assert ic.class_of(0) is None
    assert ic.CLASSES[ic.class_of(5)][2] == "<1/10"
    assert ic.CLASSES[ic.class_of(50)][2] == "4-6/10"
    assert ic.CLASSES[ic.class_of(90)][2] == "9-10/10"
    assert ic.CLASSES[ic.class_of(100)][2] == "10/10"


def _chart(tmp_path, water=None):
    return ic.IceChart(tmp_path, None, water, fake_icechart_get)


def test_day_grid_tiles_and_value(tmp_path):
    async def run():
        chart = _chart(tmp_path)
        days = await chart.times(None, NOW)
        assert PEAK in days and days[-1] == parse_instant("2026-11-08")
        if chart._reading:
            await chart._reading
        info = chart.info(NOW)
        assert info["latest_ice"] is False and info["default"] == PEAK == info["peak"]
        conc, thick, meta = await chart.grids.ready(PEAK)
        assert meta["ice_px"] > 0 and meta["max_cm"] > 50
        assert chart.grids.models == {"conc": "lightness", "thick": "lightness"}
        # The tap matches the simulator's own numbers.
        lat, lon = 60.0, 27.0
        want_c, want_t = sim_ice(lat, lon, PEAK)
        got = await chart.value(lat, lon, PEAK)
        assert abs(got["conc"] - want_c) <= 2 and abs(got["thick"] - want_t) <= 3
        assert got["day"] == "2026-02-25"
        body, final = await chart.tile(PEAK, 7, 73, 36)
        assert final and body
        img = Image.open(io.BytesIO(body))
        assert img.size == (ic.OUT, ic.OUT) and img.getchannel("A").getbbox()
        # Outside the product, and a day without ice: nothing.
        assert await chart.tile(PEAK, 7, 10, 10) == (None, True)
        assert await chart.tile(days[-1], 7, 73, 36) == (None, True)
    asyncio.run(run())


def test_cut_to_the_water(tmp_path):
    e = 512
    # Water only on the tile's left half, with an island in it, and a lake on the right.
    half = [[0, 0, e / 2, 0, e / 2, e, 0, e], [50, 50, 150, 50, 150, 150, 50, 150]]
    state = {"water": {"e": e, "water": [half], "lake": []}, "complete": True}

    async def water(z, x, y):
        return (state["water"], state["complete"]) if state["water"] is not None else None

    async def run():
        chart = _chart(tmp_path, water)
        await chart.times(None, NOW)
        body, final = await chart.tile(PEAK, 7, 73, 36)
        a = Image.open(io.BytesIO(body)).getchannel("A")
        assert final
        assert a.getpixel((400, 400)) == 0  # the dry half
        assert a.getpixel((200, 200)) == 0  # the island
        # Still downloading: nothing yet, asked again.
        state["water"] = None
        assert await chart.tile(PEAK, 8, 147, 73) == (None, False)
        # The chart has nothing here yet: drawn uncut, not kept.
        state["water"], state["complete"] = {"e": e, "water": [], "lake": []}, False
        body, final = await chart.tile(PEAK, 8, 147, 73)
        assert body and not final
    asyncio.run(run())


def test_listing_and_partial_tiles(tmp_path):
    async def water(z, x, y):
        return None

    async def run():
        reg = Overlays(tmp_path / "ov", None)
        ic.register(reg, tmp_path / "ic", None, water, fake_icechart_get)
        listing = {o["id"]: o for o in await reg.listing({ic.ID}, NOW)}
        o = listing[ic.ID]
        assert o["pickable"] and o["static"] and o["has_value"] and o["info"]["peak"] == PEAK
        # z4 is drawn without water; the path is a kept one.
        path = await reg.tile(ic.ID, PEAK, 4, 9, 4)
        assert path and not path.name.endswith(".partial.png")
        # Waiting for the water: no tile.
        assert await reg.tile(ic.ID, PEAK, 7, 73, 36) is None
    asyncio.run(run())


def test_sim_capabilities_parse():
    xml = asyncio.run(fake_icechart_get(ic.CHART_URL + "?SERVICE=WMTS&REQUEST=GetCapabilities")).decode()
    assert ic.CONC_LAYER in xml
    info = json.loads(asyncio.run(fake_icechart_get(
        ic.INFO_URL.format(layer=ic.CONC_LAYER, style="x", z=6, x=36, y=18, i=10, j=10, time="2026-02-25T00:00:00Z"))))
    assert "value" in info["features"][0]["properties"]
