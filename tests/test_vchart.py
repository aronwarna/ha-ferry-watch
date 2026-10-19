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
        # Only the region's heritage sites (shared by many blocks) were fetched.
        assert [k for k in asked if k != "heritage"] == [] and not chart._pending
        assert not any(k[:2] == key for k in chart._failed)

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
        # No key: no MML part. Heritage sites come once for the whole region.
        assert len(asked) == 2 * (len(vchart.PARTS[""]) + len(vchart.PARTS["fw"]) + len(vchart.PARTS["more"])) + 1
        assert asked.count("heritage") == 1
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
        assert any(n[2] == "Rönnskari" and n[3] == "i" for n in tile["name"])
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
    rows = {"name": [[[ "HANKO, POHJOINEN", "fin", 48120], 100.0, 100.0], [["Hanko", "fin", 48111], 200.0, 200.0]]}
    tile = vchart.make_tile(11, 0, 0, {(0, 0): vchart.layers_from_rows(rows)})
    assert [n[2] for n in tile["name"]] == ["Hanko"]


def test_names_appear_by_zoom():
    rows = {"name": [
        [["Granskär", "swe", None], 100.0, 100.0],         # island, by its ending
        [["Västerfjärden", "swe", 36490], 300.0, 300.0],    # water
        [["Stormossen", "swe", 35010], 500.0, 500.0],       # inland
        [["HANGON LENTOKENTTÄ", "fin", 35010], 700.0, 700.0],
        [["Hanko", "fin", 48111], 900.0, 900.0],            # town
    ]}

    layers = vchart.layers_from_rows(rows)

    def names(z):
        return sorted(r[2] for r in vchart.make_tile(z, 0, 0, {(0, 0): layers}).get("name", []))

    assert names(11) == ["Hanko"]
    assert names(12) == ["Hanko", "Västerfjärden"]  # open waters before islands
    assert names(13) == ["Granskär", "Hanko", "Västerfjärden"]
    assert names(15) == ["Granskär", "Hanko", "Stormossen", "Västerfjärden"]
    assert "HANGON LENTOKENTTÄ" in names(16)


def test_unlit_marks_hidden_when_zoomed_out(tmp_path):
    async def run():
        chart = VectorChart(tmp_path, None, fake_chart_layer)
        z, x, y = tile_at(60.0, 24.4, 11)
        out, _ = await chart.tile(z, x, y)
        assert out["aid"] and all(a[2] in vchart.MAJOR_AIDS for a in out["aid"])  # lighthouses, sector lights

        async def children(z, x, y):
            return [a for i in (0, 1) for j in (0, 1)
                    for a in (await chart.tile(z + 1, 2 * x + i, 2 * y + j))[0].get("aid", [])]

        near = await children(z, x, y)
        assert any(a[2] not in vchart.MAJOR_AIDS for a in near) and all(a[4] == 1 or a[2] in "ms" for a in near)
        nearer = [a for i in (0, 1) for j in (0, 1) for a in await children(12, 2 * x + i, 2 * y + j)]
        assert any(a[4] == 0 for a in nearer)  # unlit marks from zoom 13

    asyncio.run(run())


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


def _dense_rows(islands=40, per_ring=200, seed=1):
    """Old-style rows for one block: islands with depth bands and contours, and soundings."""
    import random
    rnd = random.Random(seed)
    depth, contour, sounding = [], [], []
    for _ in range(islands):
        cx, cy, r = rnd.uniform(5000, 60000), rnd.uniform(5000, 60000), rnd.uniform(300, 2000)
        rings = [[(cx + r * s * (1 + 0.2 * math.sin(7 * a)) * math.cos(a), cy + r * s * math.sin(a))
                  for a in (2 * math.pi * i / per_ring for i in range(per_ring))] for s in (1.0, 1.3, 1.6, 2.0)]
        for i, (d1, d2) in enumerate(((0, 3), (3, 6), (6, 10))):
            depth.append([[d1, d2], None, [rings[i + 1], rings[i]]])
            contour.append([[d2], None, rings[i + 1]])
    for _ in range(2000):
        sounding.append([[round(rnd.uniform(1, 60), 1)], rnd.uniform(0, 65536), rnd.uniform(0, 65536)])
    return {"depth": depth, "contour": contour, "sounding": sounding}


def test_block_file_levels_round_trip(tmp_path):
    full = vchart.layers_from_rows(_dense_rows())
    levels = vchart.make_levels(full)
    path = tmp_path / "1_2.fwc"
    vchart.write_block(path, {"v": 2, "missing": ["aid"]}, levels)
    assert vchart.read_meta(path)["missing"] == ["aid"]
    sizes = []
    for i, level in enumerate(levels):
        meta, got = vchart.read_block(path, i)
        assert set(got) == set(level)
        for key, layer in got.items():
            for name in vchart.Layer.ARRAYS[layer.kind]:
                assert getattr(layer, name) == getattr(level[key], name)
            assert layer.props == level[key].props
        sizes.append(len(got["depth"].co))
    # Zoomed-out levels are simplified copies: fewer vertices, never more.
    assert sizes[0] < sizes[1] <= sizes[2] == len(full["depth"].co)
    assert "sounding" not in levels[0] and "sounding" in levels[1]
    # About 8 bytes a vertex in memory.
    vertices = len(full["depth"].co) // 2 + len(full["contour"].co) // 2
    assert full["depth"].nbytes() + full["contour"].nbytes() < 12 * vertices


def test_old_block_files_are_converted_once(tmp_path):
    import gzip
    import json
    import os
    import time

    async def fetch(key, bbox):
        return await fake_chart_layer(key, bbox)

    z, x, y = tile_at(60.005, 24.4, 12)
    bx, by = blocks_for(z, x, y)[0]
    rows = _dense_rows(islands=5)
    rows["aid"] = [[["Viitta", 2, "K", "Testi"], 30000.0, 30000.0]]
    rows["farea"] = [[[1, 9.0], None, [[(1000, 1000), (9000, 1000), (9000, 3000), (1000, 3000)]]]]
    old = tmp_path / "blocks" / f"{bx}_{by}.json.gz"
    old.parent.mkdir(parents=True)
    with gzip.open(old, "wt") as f:
        json.dump({"v": 1, "missing": [], "layers": rows}, f, separators=(",", ":"))
    then = time.time() - 86400
    os.utime(old, (then, then))
    chart = VectorChart(tmp_path, None, fetch)
    tile, ok = asyncio.run(chart.tile(z, x, y))
    new = tmp_path / "blocks" / f"{bx}_{by}.fwc"
    assert ok and not old.exists() and new.exists()
    assert abs(new.stat().st_mtime - then) < 2  # keeps its age, so it's refreshed on schedule
    assert any(a[5] == "Testi" for a in tile["aid"])
    assert tile["depth"] and tile["contour"]
    # The old file's fairway areas (no fairway number) give way to the new "fw" part's.
    data = asyncio.run(chart.block(bx, by))
    assert (1, 9.0) not in data["farea"].props and all(len(p) == 3 for p in data["farea"].props)


def test_dense_blocks_stay_small_in_memory(tmp_path, monkeypatch):
    monkeypatch.setattr(vchart, "MEMORY_BYTES", 2_000_000)

    async def fetch(key, bbox):
        return await fake_chart_layer(key, bbox)

    async def run():
        chart = VectorChart(tmp_path, None, fetch)
        _, x, y = tile_at(60.0, 24.4, 13)
        for i in range(-6, 6):
            await chart.tile(13, x + 8 * i, y)
        assert chart._memory_bytes <= 2_000_000 or len(chart._memory) == 1

    asyncio.run(run())


def test_run_shortening_clips_the_same():
    import random
    rnd = random.Random(3)
    ring = [(rnd.uniform(-3000, 3000), rnd.uniform(-3000, 3000)) for _ in range(300)]
    layer = vchart.layers_from_rows({"depth": [[[0, 3], None, [ring]]]})["depth"]
    box = (-500, -400, 700, 600)
    full = clip_ring(layer.pts(0, layer.vo[1]), *box)
    short = clip_ring(vchart._run_pts(layer, 0, layer.vo[1], *box), *box)
    assert len(short) <= len(full)
    area = lambda pts: abs(sum(ax * by - bx * ay for (ax, ay), (bx, by) in zip(pts, pts[1:] + pts[:1]))) / 2
    assert abs(area(full) - area(short)) < 1e-6 * max(1, area(full))


def test_chart_style_fields(tmp_path):
    async def run():
        chart = VectorChart(tmp_path, None, fake_chart_layer, mml_key="test")
        t12, ok = await chart.tile(*tile_at(60.005, 24.4, 12))
        t13, _ = await chart.tile(*tile_at(59.975, 24.62, 13))
        t11, _ = await chart.tile(*tile_at(60.0, 24.45, 11))
        return t12, ok, t13, t11

    t12, ok, t13, t11 = asyncio.run(run())
    assert ok
    # Light colours from the sectors, including the all-round ones of plain lights.
    lighthouse = next(a for a in t12["aid"] if a[5] == "Porkkala majakka")
    assert lighthouse[6] == "W"
    sector_light = next(a for a in t13["aid"] if a[5] == "Kallbådan")
    assert sector_light[6] == "WRG"
    assert all((b - a) % 360 for _, _, a, b, _ in t13["sector"])  # no all-round arcs
    # Fairway class from the route data; the small-craft trunk route gets the green band.
    assert any(row[2] == 1 for row in t11["farea"])
    assert {row[1] for row in t11["fline"]} >= {1, 4}
    assert t11["band"]
    assert t13["lead"] and t13["lead"][0][0] == 234.8
    assert {row[0] for row in t12["contour"]} <= set(vchart.CONTOURS)
    assert "restrict" in t12 and t12["restrict"][0][:2] == ["01", 10]
    assert "restrict" not in t11


def test_heritage_sites(tmp_path):
    async def run():
        chart = VectorChart(tmp_path, None, fake_chart_layer, mml_key="test")
        t13, ok = await chart.tile(*tile_at(60.0, 24.58, 13))
        t11, _ = await chart.tile(*tile_at(59.97, 24.5, 11))
        t10, _ = await chart.tile(*tile_at(59.97, 24.5, 10))
        return chart, t13, ok, t11, t10

    chart, t13, ok, t11, t10 = asyncio.run(run())
    assert ok
    falken = next(h for h in t13["heritage"] if h[3] == "Falken")
    assert falken[2] == 1203 and falken[4] == 0 and falken[5] == 1 and falken[8] == 1  # a chart wreck is there too
    assert {h[2] for h in t11["heritage"]} >= {1181}
    assert "heritage" not in t10
    assert (tmp_path / "heritage").exists()
    assert "Museovirasto" in chart.status()["attribution"]


def test_too_big_pages_are_asked_for_in_halves(tmp_path):
    counts = []

    class Content:
        def __init__(self, body):
            self.body = body

        async def iter_chunked(self, n):
            for i in range(0, len(self.body), n):
                yield self.body[i:i + n]

    class Resp:
        status = 200

        def __init__(self, body):
            self.content = Content(body)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class Session:
        def get(self, url, params=None, **kw):
            count = int(params["count"])
            counts.append(count)
            if count > 500:
                return Resp(b"{" + b" " * (vchart.MAX_RESPONSE + 10) + b"}")
            start = int(params["startIndex"])
            feats = [{"geometry": {"type": "Point", "coordinates": [i, i]}, "properties": {}}
                     for i in range(start, min(start + count, 1200))]
            return Resp(json.dumps({"features": feats, "numberMatched": 1200}).encode())

    import json
    chart = VectorChart(tmp_path, Session())
    got = asyncio.run(chart._fetch_wfs("sounding", (0, 0, 1, 1)))
    assert len(got) == 1200 and counts[:3] == [2000, 1000, 500]
