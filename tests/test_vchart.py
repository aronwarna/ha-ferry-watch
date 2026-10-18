import asyncio
import math

import aiohttp

from ferrywatch import vchart
from ferrywatch.simulate import fake_chart_layer
from ferrywatch.vchart import (VectorChart, _depth_band, block_bounds, blocks_for, clip_line, clip_ring,
                               lonlat_to_mercator, near_finland, simplify)


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
    assert blocks_for(12, 2330, 1180) == [(1165, 590)]
    assert len(blocks_for(10, 582, 295)) == 4


def test_tiles_from_simulated_data(tmp_path):
    chart = VectorChart(tmp_path, None, fake_chart_layer)

    async def run():
        t12, ok12 = await chart.tile(*tile_at(60.005, 24.4, 12))
        t14, _ = await chart.tile(*tile_at(59.99, 24.47, 13))
        t9, ok9 = await chart.tile(*tile_at(60.0, 24.4, 10))
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
    assert chart.status()["blocks"] == 4  # the zoom-10 tile's four blocks (the others share them)


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


def test_only_fetches_near_finland(tmp_path):
    asked = []

    async def fetch(key, bbox):
        asked.append(key)
        return await fake_chart_layer(key, bbox)

    _, bx, by = tile_at(59.0, 15.0, 11)  # off the Swedish mainland
    assert not near_finland(block_bounds(bx, by))
    _, fx, fy = tile_at(60.0, 24.4, 11)
    assert near_finland(block_bounds(fx, fy))
    chart = VectorChart(tmp_path, None, fetch)
    tile, ok = asyncio.run(chart.tile(11, bx, by))
    assert ok and asked == [] and "land" not in tile


def test_oversized_response_is_refused(tmp_path):
    class Content:
        async def iter_chunked(self, n):
            for _ in range(400):
                yield b"x" * 65536

    class Resp:
        status = 200
        content = Content()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class Session:
        def get(self, *a, **kw):
            return Resp()

    chart = VectorChart(tmp_path, Session())
    try:
        asyncio.run(chart._fetch_wfs("sounding", (0, 0, 1, 1)))
    except ValueError as err:
        assert "MB of sounding data" in str(err)
    else:
        raise AssertionError("expected the response to be refused")


def test_slow_tile_answers_pending_and_finishes_in_background(tmp_path):
    release = None

    async def fetch(key, bbox):
        await release.wait()
        return await fake_chart_layer(key, bbox)

    async def run():
        nonlocal release
        release = asyncio.Event()
        chart = VectorChart(tmp_path, None, fetch)
        coords = tile_at(60.005, 24.4, 12)
        assert await chart.tile(*coords, wait=0.05) is None  # still downloading
        release.set()
        for _ in range(100):
            await asyncio.sleep(0.02)
            if not chart._pending:
                break
        result = await chart.tile(*coords, wait=0.05)
        assert result is not None and result[1] and result[0]["depth"]

    asyncio.run(run())


def test_queued_block_nobody_wants_is_skipped(tmp_path, monkeypatch):
    asked = []

    async def fetch(key, bbox):
        asked.append(key)
        return await fake_chart_layer(key, bbox)

    async def run():
        chart = VectorChart(tmp_path, None, fetch)
        z, x, y = tile_at(60.005, 24.4, 12)
        key = blocks_for(z, x, y)[0]
        async with chart._limit:  # another block is downloading
            assert await chart.tile(z, x, y, wait=0.01) is None
            chart._wanted[key] -= vchart.FORGET_AFTER + 1  # the map has moved on
        await asyncio.sleep(0.05)
        assert asked == [] and not chart._pending and key not in chart._failed

    asyncio.run(run())


def test_keep_downloads_every_block_once(tmp_path, monkeypatch):
    monkeypatch.setattr(vchart, "KEEP_PAUSE", 0)
    asked = []

    async def fetch(key, bbox):
        asked.append(key)
        return await fake_chart_layer(key, bbox)

    async def run():
        chart = VectorChart(tmp_path, None, fetch)
        _, bx, by = tile_at(60.0, 24.4, 11)
        blocks = [(bx, by), (bx + 1, by)]
        task = asyncio.create_task(chart.keep(blocks))
        for _ in range(200):
            await asyncio.sleep(0.01)
            if chart.keep_status and not chart.keep_status["running"]:
                break
        assert chart.keep_status == {"total": 2, "done": 2, "running": False}
        assert len(asked) == 2 * (len(vchart.PARTS[""]) + len(vchart.PARTS["more"]))  # no key: no MML part
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert chart.keep_status is None

    asyncio.run(run())


def test_keep_area_size():
    blocks = vchart.keep_blocks()
    assert 200 < len(blocks) < 500
    assert vchart.blocks_for(12, *tile_at(60.0, 24.4, 12)[1:])[0] in blocks


def test_rocks_wrecks_names_and_lights(tmp_path):
    async def run():
        chart = VectorChart(tmp_path, None, fake_chart_layer, mml_key="test")
        tile, ok = await chart.tile(*tile_at(60.005, 24.4, 13))
        assert ok
        assert tile["rock"] and {r[2] for r in tile["rock"]} <= {1, 2, 3}
        assert any(n[2] == "Rönnskari" and n[3] == "l" for n in tile["name"])
        # Finnish names come before Swedish ones, so the Finnish one wins a shared spot.
        names = [n[2] for n in tile["name"]]
        assert names.index("Rönnskari") < names.index("Rönnskär")
        assert ["LFl(2) 15s"] == [t for *_, t in tile["light"] if t.startswith("LFl")]
        zoomed_out, _ = await chart.tile(*tile_at(60.005, 24.4, 11))
        assert "rock" not in zoomed_out and all(n[3] == "s" for n in zoomed_out.get("name", []))
        assert chart.status()["rev"].endswith("mm")

    asyncio.run(run())


def test_adding_a_key_fetches_only_the_new_part(tmp_path):
    asked = []

    async def fetch(key, bbox):
        asked.append(key)
        return await fake_chart_layer(key, bbox)

    async def run():
        chart = VectorChart(tmp_path, None, fetch)
        coords = tile_at(60.005, 24.4, 12)
        tile, ok = await chart.tile(*coords)
        assert ok and "rock" not in tile
        asked.clear()
        chart.set_mml_key("abc")
        tile, ok = await chart.tile(*coords)
        assert ok and tile["rock"]
        assert sorted(set(asked)) == sorted(vchart.PARTS["mml"])

    asyncio.run(run())


def test_station_names_are_left_out():
    rows = {"name": [[[ "HANKO, POHJOINEN", "fin", 48120], 100.0, 100.0], [["Hanko", "fin", 48120], 200.0, 200.0]]}
    tile = vchart.make_tile(11, 0, 0, {(0, 0): rows})
    assert [n[2] for n in tile["name"]] == ["Hanko"]


def test_mml_features_are_converted_to_mercator():
    page = {"features": [
        {"geometry": {"type": "Point", "coordinates": [24.4, 60.0]}, "properties": {"teksti": "Rönnskär", "kieli": "swe"}},
        {"geometry": {"type": "Point", "coordinates": [24.5, 60.1]}, "properties": {"nimi": "Mäkiluoto"}},
        {"geometry": None, "properties": {}},
    ]}
    feats = vchart._mml_features(page)
    assert len(feats) == 2
    x, y = feats[0]["geometry"]["coordinates"]
    assert (round(x), round(y)) == tuple(round(v) for v in lonlat_to_mercator(24.4, 60.0))
    assert feats[1]["properties"]["teksti"] == "Mäkiluoto"
    lon, lat = vchart.mercator_to_lonlat(x, y)
    assert abs(lon - 24.4) < 1e-9 and abs(lat - 60.0) < 1e-9
