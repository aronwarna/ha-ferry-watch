import asyncio
import math

import aiohttp

from ferrywatch import vchart
from ferrywatch.simulate import fake_chart_layer
from ferrywatch.vchart import (VectorChart, _depth_band, blocks_for, clip_line, clip_ring, lonlat_to_mercator,
                               make_tile, simplify)


def tile_at(lat, lon, z):
    n = 2**z
    return z, int((lon + 180) / 360 * n), int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n)


def test_clip_ring_and_line():
    square = [(-5, -5), (5, -5), (5, 5), (-5, 5)]
    clipped = clip_ring(square, 0, 0, 10, 10)
    assert sorted(set(clipped)) == [(0, 0), (0, 5), (5, 0), (5, 5)]
    assert clip_ring(square, 20, 20, 30, 30) == []
    parts = clip_line([(-5, 1), (5, 1), (5, 20), (8, 20), (8, 5)], 0, 0, 10, 10)
    assert parts == [[(0.0, 1.0), (5.0, 1.0), (5.0, 10.0)], [(8.0, 10.0), (8.0, 5.0)]]


def test_simplify_keeps_shape_drops_noise():
    line = [(x, 0.01 * (x % 2)) for x in range(100)] + [(100, 50)]
    out = simplify(line, 0.5)
    assert out[0] == (0, 0.0) and out[-1] == (100, 50) and len(out) <= 4


def test_depth_bands():
    assert _depth_band(0, 3) == 1 and _depth_band(3, 6) == 2 and _depth_band(6, 10) == 3
    assert _depth_band(10, 20) == 4 and _depth_band(-1, 0) == 0 and _depth_band(None, None) == 4


def test_blocks_for_zoom():
    assert blocks_for(12, 2330, 1180) == [(582, 295)]
    assert len(blocks_for(9, 290, 147)) == 4


def test_tiles_from_simulated_data(tmp_path):
    chart = VectorChart(tmp_path, None, fake_chart_layer)

    async def run():
        t12, ok12 = await chart.tile(*tile_at(60.005, 24.4, 12))
        t14, _ = await chart.tile(*tile_at(59.99, 24.47, 13))
        t9, ok9 = await chart.tile(*tile_at(60.0, 24.4, 9))
        return t12, ok12, t14, t9, ok9

    t12, ok12, t14, t9, ok9 = asyncio.run(run())
    assert ok12 and ok9
    assert t12["land"] and t12["depth"] and t12["contour"]
    assert [row[0] for row in t12["depth"]] == sorted((row[0] for row in t12["depth"]), reverse=True)
    for band, *rings in t12["depth"]:
        assert all(len(r) >= 6 and len(r) % 2 == 0 for r in rings)
        assert all(-vchart.TILE_BUFFER - 1 <= v <= vchart.EXTENT + vchart.TILE_BUFFER + 1 for r in rings for v in r)
    lighthouse = [a for a in t12["aid"] if a[2] == "m"]
    assert lighthouse and lighthouse[0][4] == 1 and lighthouse[0][5] == "Porkkala majakka"
    assert "sounding" in t14 and "sounding" not in t12 and "aid" not in t9
    assert chart.status()["blocks"] == 4  # the zoom-9 tile's four blocks (the others share them)


def test_failed_fairway_data_still_draws_depths(tmp_path):
    async def flaky(key, bbox):
        if key == "aid":
            raise aiohttp.ClientError("HTTP 503")
        return await fake_chart_layer(key, bbox)

    chart = VectorChart(tmp_path, None, flaky)
    tile, ok = asyncio.run(chart.tile(*tile_at(60.005, 24.4, 12)))
    assert ok and tile["depth"] and "aid" not in tile
    assert chart.errors == {"aid": "HTTP 503"}


def test_failed_depths_give_no_land(tmp_path):
    async def down(key, bbox):
        raise aiohttp.ClientError("HTTP 403")

    chart = VectorChart(tmp_path, None, down)
    tile, ok = asyncio.run(chart.tile(*tile_at(60.005, 24.4, 12)))
    assert not ok and "land" not in tile and "depth" not in tile


def test_land_only_where_finland_is(tmp_path):
    # A tile off Tallinn has depth data from the simulation but lies outside Finnish waters.
    chart = VectorChart(tmp_path, None, fake_chart_layer)
    tile, _ = asyncio.run(chart.tile(*tile_at(59.5, 24.75, 12)))
    assert "land" not in tile
    x, y = lonlat_to_mercator(24.4, 60.0)
    assert abs(x - 2716195.6) < 1 and abs(y - 8399737.9) < 1
