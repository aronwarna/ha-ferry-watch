import asyncio
import json
import math
import os
import time

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
    # Soundings from zoom 12, sparser than at 13; zoomed out only the major lights.
    assert "sounding" in t14 and 0 < len(t12["sounding"]) <= 36 and "sounding" not in t9  # one a 128-unit cell
    assert all(a[2] in vchart.MAJOR_AIDS for a in t9["aid"])
    assert chart.status()["blocks"] == 4  # the zoom-10 tile's four blocks (the others share them)


def test_cut_tiles_are_kept_on_disk_until_their_blocks_change(tmp_path):
    import gzip
    chart = VectorChart(tmp_path, None, fake_chart_layer)
    coords = tile_at(60.005, 24.4, 12)

    async def run():
        first, ok = await chart.tile_gz(*coords)
        assert ok and chart.tiles_from_disk == 0
        chart._forget()  # nothing in RAM: the second answer comes from the disk alone
        again, ok2 = await chart.tile_gz(*coords)
        assert ok2 and again == first and chart.tiles_from_disk == 1 and not chart._memory
        assert json.loads(gzip.decompress(again))["depth"]
        # A block part fetched again (a newer file) makes the tile be cut again.
        part = chart._path(*blocks_for(*coords)[0], "")
        later = time.time() + 5
        os.utime(part, (later, later))
        await chart.tile_gz(*coords)
        assert chart.tiles_from_disk == 1

    asyncio.run(run())
    stale = tmp_path / "tiles" / "old-rev" / "12"
    stale.mkdir(parents=True)
    (stale / "1_2.json.gz").write_bytes(b"x")
    chart.prune()
    assert not stale.exists() and list((tmp_path / "tiles" / chart._rev()).glob("*/*.json.gz"))


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
    # A tile off Tallinn lies outside Finnish waters: no Finnish land there (without the
    # foreign stand-ins, so nothing from foreign.py either).
    async def finnish_only(key, bbox):
        return await fake_chart_layer(key, bbox)

    chart = VectorChart(tmp_path, None, finnish_only)
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
        assert {k: chart.keep_status[k] for k in ("total", "done", "running", "failed")} == \
            {"total": 2, "done": 2, "running": False, "failed": 0}
        assert chart.keep_status["last_ok"]
        # No key: no Land Survey parts. Each source once a block; heritage sites once for the region.
        per_block = sum(len(vchart.source_names(key)) for part in chart._parts() for key in vchart.PARTS[part])
        # A stand-in without foreign stand-ins (see foreign.py): no foreign part either.
        assert {p for p in vchart.PARTS if p not in vchart.MML_PARTS and p != vchart.FOREIGN_PART} == \
            set(chart._parts())
        assert len(asked) == 2 * per_block + 1
        assert asked.count("heritage") == 1
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert chart.keep_status is None

    asyncio.run(run())


def test_keep_moves_past_a_failing_layer(tmp_path, monkeypatch):
    monkeypatch.setattr(vchart, "KEEP_PAUSE", 0)

    async def fetch(key, bbox):
        if key.split(".")[0] in vchart.PARTS["tss"]:  # the whole part fails
            raise aiohttp.ClientConnectionError("Connection reset by peer")
        return await fake_chart_layer(key, bbox)

    async def run():
        chart = VectorChart(tmp_path, None, fetch)
        _, bx, by = tile_at(60.0, 24.4, 11)
        task = asyncio.create_task(chart.keep([(bx, by), (bx + 1, by)]))
        for _ in range(300):
            await asyncio.sleep(0.01)
            if chart.keep_status and not chart.keep_status["running"]:
                break
        # Both areas were tried; the one part that failed waits for its retry instead of
        # holding the progress at nought.
        assert chart.keep_status["done"] == 2 and chart.keep_status["failed"] == 2
        assert not chart.keep_status["running"]
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())


def test_dropped_connection_is_asked_again(tmp_path, monkeypatch):
    monkeypatch.setattr(vchart, "GET_RETRIES", (0, 0))
    calls = []

    async def once(self, url, params, what):
        calls.append(url)
        if len(calls) < 3:
            raise aiohttp.ClientOSError(104, "Connection reset by peer")
        return bytearray(b"{}")

    monkeypatch.setattr(VectorChart, "_get_once", once)
    chart = VectorChart(tmp_path, None)
    assert asyncio.run(chart._get_bytes("https://example.org", None, "depth")) == bytearray(b"{}")
    assert len(calls) == 3


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
        assert "rock" not in zoomed_out and all(n[2] in vchart.COASTAL_TOWNS | vchart.BIG_WATERS
                                                for n in zoomed_out.get("name", []))
        rev = chart.status()["rev"]
        chart.set_mml_key("")
        assert chart.status()["rev"] != rev  # tiles cached without the key's layers are dropped

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
        # Only the Land Survey's parts, each source once.
        expected = [key if tag is None else f"{key}.{tag}" for part in vchart.MML_PARTS
                    for key in vchart.PARTS[part] for tag, _ in vchart.source_names(key)]
        assert sorted(asked) == sorted(expected)

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


def test_mml_features_are_projected_from_lonlat():
    page = {"features": [
        {"geometry": {"type": "Point", "coordinates": [24.4, 60.0]}, "properties": {"teksti": "Rönnskär", "kieli": "swe"}},
        {"geometry": {"type": "Point", "coordinates": [24.5, 60.1]}, "properties": {"nimi": "Mäkiluoto"}},
        {"geometry": None, "properties": {}},
    ]}
    feats = vchart._mml_features(page)
    first = next(feats)
    assert first["properties"]["teksti"] == "Rönnskär"
    rest = list(feats)
    assert len(rest) == 1 and rest[0]["properties"]["teksti"] == "Mäkiluoto"
    assert page["features"] == []  # taken out of the page as they're used, so it's never held twice
    _, bx, by = tile_at(60.0, 24.4, 11)
    b = vchart.LayerBuilder("point")
    vchart.build_into(b, bx, by, "name", [first], lonlat=True)
    x, y = vchart.to_block(bx, by)(*lonlat_to_mercator(24.4, 60.0))
    assert (b.layer.co[0], b.layer.co[1]) == (round(x), round(y))
    lon, lat = vchart.mercator_to_lonlat(*lonlat_to_mercator(24.4, 60.0))
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
    assert "sounding" in levels[0] and "sounding" in levels[1]  # soundings from zoom 11
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


def test_coastal_defences(tmp_path):
    async def run():
        chart = VectorChart(tmp_path, None, fake_chart_layer, mml_key="test")
        island13, _ = await chart.tile(*tile_at(60.011, 24.404, 13))
        island12, _ = await chart.tile(*tile_at(60.011, 24.404, 12))
        inland, _ = await chart.tile(*tile_at(60.30, 24.40, 13))
        return island13, island12, inland

    island13, island12, inland = asyncio.run(run())
    gun = next(h for h in island13["heritage"] if h[2] == 2101)
    assert gun[5] == 5 and gun[9] == "gun positions, trenches" and gun[8] == 0
    assert not any(h[5] == 5 for h in island12.get("heritage", ()))  # land sites only up close
    assert not any(h[2] == 2103 for h in inland.get("heritage", ()))  # far from the sea


def test_coastal_filter_keeps_sites_near_the_shore():
    sea = vchart.layers_from_rows({"depth": [[[5, 10], None, [[(0, 0), (30000, 0), (30000, 65536), (0, 65536)]]]]})["depth"]
    b = vchart.LayerBuilder("point")
    shore, inland, wreck = (1, None, 1, 5, None, None, "bunkers"), (2, None, 1, 5, None, None, None), \
        (3, None, 0, 1, None, None, None)
    b.point(shore, 31500, 20000)
    b.point(inland, 60000, 20000)
    b.point(wreck, 62000, 40000)  # wrecks are kept wherever they are
    kept = vchart.coastal_filter(b.layer, sea)
    assert [kept.props[kept.pi[i]][0] for i in range(kept.n)] == [1, 3]


def test_subtypes_in_english():
    assert vchart._subtype("tykkiasemat, taisteluhaudat, tykkiasemat") == "gun positions, trenches"
    assert vchart._subtype("hylyt (metalli)") == "metal wreck"
    assert vchart._subtype("kummalliset") == "kummalliset"
    assert vchart._subtype("") is None


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


# --- routeing, land detail, layer discovery and overviews ---------------------------------

def _tiles(chart, *spots):
    async def run():
        return [await chart.tile(*tile_at(lat, lon, z)) for z, lat, lon in spots]
    return asyncio.run(run())


def test_traffic_separation_pilots_and_signs(tmp_path):
    chart = VectorChart(tmp_path, None, fake_chart_layer)
    (tss, ok), (z10, _), (speed, _), (nowake, _), (anchor, _), (call, _) = _tiles(
        chart, (12, 59.9, 24.4), (10, 59.95, 24.62), (13, 60.034, 24.395), (13, 60.002, 24.45),
        (13, 60.032, 24.475), (11, 59.947, 24.30))
    assert ok
    lanes = sorted((row[0], row[1] or 0) for row in tss["tss"])
    # Both spellings of the separation zone are fetched; the zone is kept once.
    assert lanes == [("lane", 90.0), ("lane", 270.0), ("zone", 0)]
    assert all(len(ring) >= 6 for row in tss["tss"] for ring in row[2:])
    assert {row[0] for row in tss["tssline"]} == {"bnd"}
    assert z10["pilot"] and len(z10["pilot"][0]) == 2 and "sign" not in z10
    assert any(row[0] == "lane" for row in z10["tss"])
    assert [s[2:] for s in speed["sign"] if s[2] == "speed"] == [["speed", 10, "Porkkalan salmi"]]
    assert any(s[2:4] == ["nowake", None] for s in nowake["sign"])
    assert any(s[2] == "noanchor" and s[4] == "Rönnskär" for s in anchor["sign"])
    assert any(c[2:] == ["Porkkala", "71", 180] for c in call["call"])
    assert any(row[0] == "call" for row in call["tssline"])
    assert chart.status()["layers"]["missing"] == []


def test_sign_types():
    assert vchart._sign((11, None, 8, "x")) == ("speed", 8)
    assert vchart._sign((99, "Aallokon aiheuttamisen kielto", 0, None)) == ("nowake", None)
    assert vchart._sign((None, None, None, "Alikulkukorkeus")) == ("height", None)
    assert vchart._sign((7, "Jotain muuta", 2.5, None)) == ("other", 2.5)


def test_land_detail(tmp_path):
    chart = VectorChart(tmp_path, None, fake_chart_layer, mml_key="test")
    (h12, ok), (h13, _), (h14, _), (roads12, _), (path13, _), (sea13, _), (lake11, _), (shed14, _), \
        (church13, _), (rail12, _), (h11, _), (houses13, _) = _tiles(
            chart, (12, 60.11, 24.43), (13, 60.11, 24.43), (14, 60.11, 24.43), (12, 60.12, 24.40),
            (13, 60.006, 24.40), (13, 60.05, 24.40), (11, 60.16, 24.30), (14, 60.0755, 24.395),
            (13, 60.122, 24.45), (12, 60.205, 24.30), (11, 60.11, 24.43), (13, 60.089, 24.42))
    assert ok
    # Land contours: every 20 m at zoom 12, 10 m at 13, all from 14.
    assert {m for m, _ in h12["height"]} <= {20, 40, 60} and h12["height"]
    assert {m for m, _ in h13["height"]} <= set(range(10, 70, 10)) and 30 in {m for m, _ in h13["height"]}
    assert {15, 25} <= {m for m, _ in h14["height"]}
    assert "height" not in h11 and "road" not in h11
    # Masts with their heights, one from the separate height label.
    assert sorted(m[3] for m in h12["landmark"] if m[2] == "mast") == [59, 82, 131]
    # Roads: major and minor at zoom 12, paths from 13, ice roads never.
    assert {r[0] for r in roads12["road"]} == {1, 2}
    assert {r[0] for r in path13["road"]} == {3}
    assert "road" not in sea13
    assert rail12["rail"] and len(rail12["rail"][0][0]) >= 4
    assert lake11["lake"] and all(len(ring) >= 6 for rings in lake11["lake"] for ring in rings)
    # Big buildings by their outline, small ones as points, the church as a landmark.
    assert [row[0] for row in shed14["buildarea"]] == ["industry"]
    assert any(m[2] == "church" for m in church13["landmark"])
    assert len(houses13["building"]) >= 5 and {b[2] for b in houses13["building"]} == {1}
    assert chart.status()["layers"]["missing"] == []


def test_join_heights():
    marks = vchart.LayerBuilder("point")
    marks.point(("mast", None, None, None), 1000, 1000)
    marks.point(("mast", "120", None, "TV"), 5000, 5000)
    marks.point(("chimney", None, None, None), 9000, 9000)
    labels = vchart.LayerBuilder("point")
    labels.point(("mast", None, "(75)"), 1100, 1050)
    labels.point(("mast", 40, None), 9050, 9000)  # beside the chimney, but a mast's height
    out = vchart.join_heights(marks.layer, labels.layer)
    assert [out.props[out.pi[i]] for i in range(out.n)] == [("mast", 75, None), ("mast", 120, "TV"),
                                                             ("chimney", None, None)]


def test_layers_a_service_lacks_are_empty_and_complete(tmp_path):
    asked = []
    lacking = {"rajoitettu:PrecautionaryArea_A", "rajoitettu:TrafficSeparationZone_A", "masto", "avoin:Rdocal_P"}

    async def fetch(key, bbox):
        asked.append(key)
        return await fake_chart_layer(key, bbox)

    fetch.catalog = lambda service: [n for n in fake_chart_layer.catalog(service) if n not in lacking]
    chart = VectorChart(tmp_path, None, fetch, mml_key="test")
    (tss, ok), (marks, ok12) = _tiles(chart, (12, 59.9, 24.4), (12, 60.11, 24.43))
    assert ok and ok12 and not chart.errors
    assert "tss.prec" not in asked and "call" not in asked and "landmark.mast" not in asked
    # The misspelt zone is all there is: asked for once a block, drawn once.
    assert asked.count("tss.zone") == asked.count("tss.lane") == 2
    assert [r[0] for r in tss["tss"]].count("zone") == 1
    assert not any(m[2] == "mast" for m in marks.get("landmark", ()))
    assert set(chart.status()["layers"]["missing"]) == lacking
    bx, by = blocks_for(*tile_at(59.9, 24.4, 12))[0]
    meta = vchart.read_meta(tmp_path / "blocks" / f"{bx}_{by}.tss.fwc")
    assert meta["missing"] == [] and "rajoitettu:PrecautionaryArea_A" in meta["absent"]


def test_unknown_layer_reply_counts_as_absent(tmp_path):
    async def fetch(key, bbox):
        if key == "tss.prec":
            raise vchart._HTTPError(400, '<ows:Exception exceptionCode="InvalidParameterValue" locator="typeName">'
                                         '<ows:ExceptionText>Feature type rajoitettu:PrecautionaryArea_A unknown')
        if key == "pilot":
            raise vchart._HTTPError(503, "Service Unavailable")
        return await fake_chart_layer(key, bbox)

    chart = VectorChart(tmp_path, None, fetch)  # no catalog: every name is tried
    tile, ok = _tiles(chart, (12, 59.9, 24.4))[0]
    assert ok and tile["tss"]
    assert "tss" not in chart.errors and chart.errors["pilot"].startswith("HTTP 503")
    assert chart.status()["layers"]["missing"] == ["rajoitettu:PrecautionaryArea_A"]


def test_depths_are_never_taken_for_absent(tmp_path):
    async def fetch(key, bbox):
        if key == "depth":
            raise vchart._HTTPError(400, "Feature type rajoitettu:DepthArea_A unknown")
        return await fake_chart_layer(key, bbox)

    fetch.catalog = lambda service: [n for n in fake_chart_layer.catalog(service) if not n.endswith("DepthArea_A")]
    chart = VectorChart(tmp_path, None, fetch)
    tile, ok = _tiles(chart, (12, 60.005, 24.4))[0]
    assert not ok and "depth" not in tile and "depth" in chart.errors  # tried again later, not a blank block


def test_capabilities_parsing_and_matching():
    caps = """<?xml version="1.0"?><wfs:WFS_Capabilities><ows:ServiceIdentification><ows:Title>T</ows:Title>
      </ows:ServiceIdentification><wfs:FeatureTypeList>
      <wfs:FeatureType><wfs:Name>rajoitettu:DepthArea_A</wfs:Name><wfs:Title>Depth</wfs:Title></wfs:FeatureType>
      <FeatureType><Name> rajoitettu:TrafficSeaparationZone_A </Name></FeatureType>
      </wfs:FeatureTypeList><fes:Filter_Capabilities><fes:Functions><fes:Function name="x"/></fes:Functions>
      </fes:Filter_Capabilities></wfs:WFS_Capabilities>"""
    assert vchart.wfs_type_names(caps) == {"rajoitettu:DepthArea_A", "rajoitettu:TrafficSeaparationZone_A"}
    assert vchart._wfs_match("rajoitettu:depthArea_A", {"DepthArea_A"}) == "DepthArea_A"
    assert vchart._wfs_match("avoin:Pontoon_L", {"rajoitettu:DepthArea_A"}) is None
    assert vchart._mml_match("korkeuskayra", {"Korkeus_kayra", "rakennus"}) == "Korkeus_kayra"
    assert vchart._mml_match("masto", {"mastonkorkeus"}) is None
    url, params = vchart._with_limit("https://x/items?limit=1000&offset=2000&api-key=k", None, 500)
    assert params is None and "limit=500" in url and "offset=2000" in url and "limit=1000" not in url


def test_misread_capabilities_are_not_trusted(tmp_path):
    chart = VectorChart(tmp_path, None)
    chart._set_caps(vchart.TRAFICOM_WFS, {"something:Else"}, save=False)
    assert vchart.TRAFICOM_WFS not in chart._caps
    assert chart._resolve("rajoitettu:DepthArea_A") == "rajoitettu:DepthArea_A"
    chart._set_caps(vchart.TRAFICOM_WFS, {"rajoitettu:DepthArea_A"})
    assert chart._resolve("rajoitettu:PrecautionaryArea_A") is None
    again = VectorChart(tmp_path, None)  # kept on disk across restarts
    again._load_caps()
    assert again._resolve("rajoitettu:PrecautionaryArea_A") is None and again._resolve("rajoitettu:DepthArea_A")


def test_overview_tiles(tmp_path, monkeypatch):
    chart = VectorChart(tmp_path, None, fake_chart_layer, mml_key="test")
    # Blocks get on disk as the map is looked at zoomed in; overviews are made from them.
    _tiles(chart, *[(10, lat, lon) for lat in (59.85, 59.95, 60.05) for lon in (24.1, 24.4, 24.7)])
    (t9, ok9), (t8, ok8), (empty, ok_empty), (sea, ok_sea) = _tiles(
        chart, (9, 60.0, 24.4), (8, 60.0, 24.4), (9, 65.0, 25.0), (8, 55.0, 18.0))
    for t in (t9, t8):
        assert t["land"] and t["depth"] and 0 < t["cover"] < 1
        assert t["tss"] and {row[0] for row in t["tss"]} <= set(vchart.OVERVIEW_TSS)
        assert t["contour"] and {row[0] for row in t["contour"]} <= set(vchart.OVERVIEW_CONTOURS)
        assert {row[1] for row in t["fline"]} == {1}  # merchant fairways only, the class carried along
        assert t["aid"] and all(a[2] in vchart.MAJOR_AIDS for a in t["aid"])
        assert {n[2] for n in t.get("name", ())} <= {"Suomenlahti", "Kirkkonummi"}  # seas and towns only
        for key in ("sounding", "rock", "height", "building", "sign", "heritage", "farea"):
            assert key not in t
    assert "Suomenlahti" in [n[2] for n in t8["name"]]  # (at 59.80, south of the zoom-9 tile)
    assert not ok9 and not ok8  # not every block of them is there yet
    assert empty == {"e": 512, "cover": 0} and not ok_empty  # nothing fetched up north
    assert sea == {"e": 512} and ok_sea  # nothing Finnish out there at all
    assert list((tmp_path / "overview").iterdir())
    assert not chart._memory_bytes or len(chart._memory) <= vchart.MEMORY_BLOCKS  # nothing kept for overviews
    # Kept, and read back from disk after a restart.
    again = VectorChart(tmp_path, None, fake_chart_layer, mml_key="test")
    assert json.dumps(_tiles(again, (9, 60.0, 24.4))[0][0]) == json.dumps(t9)
    # Another block of it fetched: shown as it was for a while (and not complete), then cut again.
    _tiles(again, (10, 60.15, 24.1))
    (stale, ok), = _tiles(again, (9, 60.0, 24.4))
    assert json.dumps(stale) == json.dumps(t9) and not ok
    monkeypatch.setattr(vchart, "OVERVIEW_RECUT", 0)
    (recut, _), = _tiles(again, (9, 60.0, 24.4))
    assert recut["cover"] > t9["cover"]
    # Without the Land Survey key its names aren't on overview tiles either.
    (no_key, _), = _tiles(VectorChart(tmp_path, None, fake_chart_layer), (9, 60.0, 24.4))
    assert "name" not in no_key and no_key["depth"]


def test_ram_is_let_go_when_nobody_looks(tmp_path, monkeypatch):
    monkeypatch.setattr(vchart, "IDLE_FORGET", 0.2)

    async def run():
        chart = VectorChart(tmp_path, None, fake_chart_layer)
        await chart.tile(*tile_at(60.005, 24.4, 12))
        await chart.tile(*tile_at(60.0, 24.4, 9))
        assert chart._memory
        for _ in range(3):  # still being looked at: kept
            await asyncio.sleep(0.1)
            await chart.tile(*tile_at(60.005, 24.4, 12))
        assert chart._memory
        await asyncio.sleep(0.4)
        assert not chart._memory and not chart._coast and chart._memory_bytes == 0
        tile, ok = await chart.tile(*tile_at(60.005, 24.4, 12))  # read back from disk
        assert ok and tile["depth"] and chart._memory

    asyncio.run(run())


def test_overview_level_is_skipped_by_read_block(tmp_path):
    full = vchart.layers_from_rows(_dense_rows(islands=5))
    levels = vchart.make_levels(full)
    old, new = tmp_path / "old.fwc", tmp_path / "new.fwc"
    vchart.write_block(old, {"v": 2, "missing": []}, levels)
    vchart.write_block(new, {"v": 2, "missing": ["aid"]}, levels, vchart.make_overview(full))
    assert vchart.read_meta(new)["missing"] == ["aid"]
    for i in range(len(vchart.LEVELS)):
        a, b = vchart.read_block(old, i)[1], vchart.read_block(new, i)[1]
        assert set(a) == set(b) and all(a[k].co == b[k].co for k in a)
    ov_new, ov_old = vchart.read_overview(new), vchart.read_overview(old)  # old files: from their first level
    assert set(ov_new) == {"depth", "contour"} == set(ov_old)
    assert len(ov_new["depth"].co) < len(levels[0]["depth"].co)
    assert all(float(p[0]) in vchart.OVERVIEW_CONTOURS for p in ov_new["contour"].props)


def test_dense_land_block_stays_small(tmp_path):
    import random
    rnd = random.Random(2)
    _, bx, by = tile_at(60.1, 24.4, 11)
    x0, y0, x1, y1 = block_bounds(bx, by)

    def pt():
        return rnd.uniform(x0, x1), rnd.uniform(y0, y1)

    builders = {k: vchart.LayerBuilder(vchart.SOURCES[k][2]) for k in ("height", "building", "buildarea")}
    contours = []
    for _ in range(600):
        cx, cy = pt()
        r = rnd.uniform(100, 600)
        contours.append({"geometry": {"type": "LineString", "coordinates": [
            [cx + r * math.cos(a) * (1 + 0.1 * math.sin(9 * a)), cy + r * math.sin(a)]
            for a in (2 * math.pi * i / 400 for i in range(401))]},
            "properties": {"korkeusarvo": 5 * rnd.randint(1, 12)}})
    buildings = []
    for _ in range(6000):
        cx, cy = pt()
        w, h = rnd.uniform(8, 160), rnd.uniform(8, 100)  # web-mercator metres: half that on the ground
        buildings.append({"geometry": {"type": "Polygon", "coordinates": [[[cx, cy], [cx + w, cy], [cx + w, cy + h],
                                                                            [cx, cy + h], [cx, cy]]]},
                          "properties": {"kohdeluokka": 42211}})
    vchart.build_into(builders["height"], bx, by, "height", contours)
    vchart.build_into(builders["building"], bx, by, "building", buildings, extra=builders["buildarea"])
    built = vchart.finish_part({k: b.layer for k, b in builders.items()})
    kept = len(built["height"].co) // 2
    assert kept < 600 * 401 * 0.7  # full detail simplified by about half a metre
    assert built["building"].n == 6000 and built["buildarea"].n > 0
    assert {built["building"].props[i][1] for i in built["building"].pi} == {1, 2, 3}
    levels = vchart.make_levels(built)
    path = tmp_path / f"{bx}_{by}.land.fwc"
    vchart.write_block(path, {"v": 2, "missing": []}, levels, vchart.make_overview(built))
    outline = len(built["buildarea"].co) // 2  # every building's corners, for zoom 14 and closer
    assert built["buildarea"].n > 5900 and outline <= 5 * built["buildarea"].n
    assert path.stat().st_size < 8 * (kept + 6000 + outline)  # compressed: under the arrays' 8 bytes a vertex
    assert levels[0] == {}  # nothing of it zoomed out (zoom 11 and below)
    # Zoom 13 keeps only the biggest outlines; the rest are squares there.
    assert 0 < levels[1]["buildarea"].n < built["buildarea"].n / 4
    assert all(vchart._multiple(p[0], 10) for p in levels[1]["height"].props)
    assert len(levels[1]["height"].co) < len(built["height"].co)
    assert sum(L.nbytes() for L in built.values()) < 10 * (kept + 6000) + 14 * outline + 50_000
    assert vchart.read_overview(path) == {}


def test_major_light_characters_zoomed_out(tmp_path):
    async def run():
        chart = VectorChart(tmp_path, None, fake_chart_layer, mml_key="test")
        t11, _ = await chart.tile(*tile_at(60.005, 24.40, 11))
        t13, _ = await chart.tile(*tile_at(60.005, 24.40, 13))
        return t11, t13

    t11, t13 = asyncio.run(run())
    assert any(t == "LFl(2) 15s" for _, _, t in t11.get("light", ()))  # Porkkala lighthouse
    assert len(t11.get("light", ())) < len(t13.get("light", ())) or len(t13["light"]) == 1


def test_simulated_coast_has_cables_and_depths(tmp_path):
    from ferrywatch import simscene
    chart = VectorChart(tmp_path, None, fake_chart_layer, mml_key="test")
    lon, lat = simscene._lonlat(2.6, 6.9)  # on the cable across the western bay
    tile, ok = asyncio.run(chart.tile(*tile_at(lat, lon, 12)))
    assert ok
    assert tile["cable"] and all(kind == "cable" and len(line) >= 4 for kind, line in tile["cable"])
    assert {row[0] for row in tile["depth"]} >= {1, 2, 3, 4}
    assert tile["rock"] and tile["sounding"]


def _covered(rows, x, y):
    """The rows (of [first, ...rings] or [[...rings]]) whose rings hold a point, even-odd."""
    def inside(ring):
        hit = False
        pts = list(zip(ring[0::2], ring[1::2]))
        j = len(pts) - 1
        for i, (xi, yi) in enumerate(pts):
            xj, yj = pts[j]
            if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
                hit = not hit
            j = i
        return hit
    return [row for row in rows if sum(inside(r) for r in row if isinstance(r, list)) % 2]


def _square(x0, y0, x1, y1):
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


def test_dredged_and_unsurveyed_holes_are_water():
    # A block off Porkkala's coast (inside Finnish waters, away from the sea border) whose
    # depth area has two holes: a dredged area 11 m deep and an unsurveyed area.
    _, bx, by = tile_at(59.95, 24.3, vchart.BLOCK_Z)
    assert not vchart.near_sea_border(bx, by)
    whole, dredged, unsurveyed = _square(0, 0, 65536, 65536), _square(8000, 8000, 24000, 24000), \
        _square(40000, 40000, 56000, 56000)
    full = vchart.layers_from_rows({
        "depth": [[[10, 20], None, [whole, dredged, unsurveyed]]],
        "tss": [[["dredged", None, 11.0], None, [dredged]], [["unsurveyed", None, None], None, [unsurveyed]]],
    })
    levels = vchart.make_levels(full)
    overview = vchart.make_overview(full)
    for z in (8, 9, 10, 11, 12, 14):
        data = overview if z <= vchart.OVERVIEW_LEVEL[0] else levels[vchart.level_for(z)]
        n = 2 ** (z - vchart.BLOCK_Z)
        if z >= vchart.BLOCK_Z:
            # The tile with the dredged area's middle in it (and, at zoom 11, the others').
            size = vchart.UNITS / n
            tx, ty = bx * n + int(16000 // size), by * n + int(16000 // size)
            ox, oy = (tx - bx * n) * size, (ty - by * n) * size
            k = vchart.EXTENT / size
            spots = {"dredged": (16000, 16000), "unsurveyed": (48000, 48000), "deep": (32000, 32000)}
            spots = {name: ((px - ox) * k, (py - oy) * k) for name, (px, py) in spots.items()
                     if z == vchart.BLOCK_Z or name == "dredged"}
        else:
            m = 2 ** (vchart.BLOCK_Z - z)
            tx, ty = bx // m, by // m
            k = vchart.EXTENT / (vchart.UNITS * m)
            ox, oy = (bx - tx * m) * vchart.UNITS, (by - ty * m) * vchart.UNITS
            spots = {"dredged": ((ox + 16000) * k, (oy + 16000) * k),
                     "unsurveyed": ((ox + 48000) * k, (oy + 48000) * k)}
        tile = vchart.make_tile(z, tx, ty, {(bx, by): data})
        for name, (x, y) in spots.items():
            bands = [row[0] for row in _covered(tile["depth"], x, y)]
            want = {"dredged": [4], "unsurveyed": [vchart.UNSURVEYED], "deep": [4]}[name]
            assert bands == want, (z, name, bands)
        kinds = {row[0] for row in tile.get("tss", ())}
        if z >= vchart.WATER_TSS_LIMITS_Z:
            assert kinds == {"dredged", "unsurveyed"} if z <= vchart.BLOCK_Z else "dredged" in kinds
        else:
            assert not kinds & set(vchart.WATER_TSS)  # zoomed out, only the water


def test_dredged_area_takes_the_band_of_its_least_depth():
    whole, hole = _square(0, 0, 65536, 65536), _square(8000, 8000, 24000, 24000)
    layers = vchart.layers_from_rows({
        "depth": [[[10, 20], None, [whole, hole]]],
        "tss": [[["dredged", None, 5.5], None, [hole]]],
    })
    tile = vchart.make_tile(11, 0, 0, {(0, 0): layers})
    assert [row[0] for row in _covered(tile["depth"], 16000 * 512 / 65536, 16000 * 512 / 65536)] == [2]
    # A tss part from before PART_REV (no least depth): deeper water, and fetched again.
    old = vchart.layers_from_rows({"depth": [[[10, 20], None, [whole, hole]]],
                                   "tss": [[["dredged", None], None, [hole]]]})
    tile = vchart.make_tile(11, 0, 0, {(0, 0): old})
    assert [row[0] for row in _covered(tile["depth"], 125, 125)] == [4]
    assert vchart._fetch_again({"missing": []}, "1_2.tss.fwc")
    assert not vchart._fetch_again({"missing": [], "rev": vchart.PART_REV["tss"]}, "1_2.tss.fwc")
    assert not vchart._fetch_again({"missing": []}, "1_2.fwc")
    assert vchart._fetch_again({"missing": ["depth"]}, "1_2.fwc")


def test_sea_border_gap_is_sea_and_islands_land():
    # A block on the Gulf of Finland's sea border south of Porkkala, where Traficom's depths
    # stop short of FINNISH_WATERS's border: they cover the block's north half only, with an
    # island in them.
    _, bx, by = tile_at(59.74, 24.3, vchart.BLOCK_Z)
    assert vchart.near_sea_border(bx, by)
    north, island = _square(0, 0, 65536, 30000), _square(20000, 10000, 26000, 16000)
    layers = vchart.layers_from_rows({"depth": [[[20, 50], None, [north, island]]]})
    k = vchart.EXTENT / vchart.UNITS
    tile = vchart.make_tile(11, bx, by, {(bx, by): layers})
    # Find a spot in the gap that's inside FINNISH_WATERS (the border crosses the block).
    gap = next((x, y) for y in range(34000, 65536, 2000) for x in range(1000, 65536, 4000)
               if vchart._in_finnish_waters(bx, by, x, y))
    gx, gy = gap[0] * k, gap[1] * k
    assert [row[0] for row in _covered(tile["depth"], gx, gy)] == [4]
    assert not any(_covered([[r]], gx, gy) for r in tile["land"])
    ix, iy = 23000 * k, 13000 * k
    assert not _covered(tile["depth"], ix, iy)
    assert any(_covered([[r]], ix, iy) for r in tile["land"])
    # Away from the border, land is the whole Finnish area under the depths, as before.
    assert not vchart.near_sea_border(*tile_at(60.1, 24.4, vchart.BLOCK_Z)[1:])
    assert not vchart.near_sea_border(*tile_at(65.8, 24.2, vchart.BLOCK_Z)[1:])  # Tornio


def test_sample_has_tiles_and_block_levels(tmp_path):
    chart = VectorChart(tmp_path, None, fake_chart_layer)
    sample = asyncio.run(chart.sample(24.4, 60.005, zooms=(12, 10)))
    z12 = "/".join(map(str, tile_at(60.005, 24.4, 12)))
    assert set(sample["tiles"]) == {z12, "/".join(map(str, tile_at(60.005, 24.4, 10)))}
    assert sample["tiles"][z12]["tile"]["depth"]
    levels = sample["block"]["levels"]
    assert set(levels) == {"overview", "0", "1", "2"}
    assert levels["0"]["layers"]["depth"]["rows"] == len(levels["0"]["depth"]) > 0
    assert levels["2"]["depth"] is None  # the full detail only as counts
    json.dumps(sample)  # saved as a file


def _ring(cx, cy, r, n=48, wobble=0.15, phase=0.0):
    return [(cx + r * (1 + wobble * math.sin(5 * a + phase)) * math.cos(a),
             cy + r * (1 + wobble * math.sin(5 * a + phase)) * math.sin(a))
            for a in (2 * math.pi * i / n for i in range(n))]


def test_zoomed_out_tiles_keep_shallows_and_islands():
    """A real-shaped archipelago: one deep area over the whole block with hundreds of holes,
    25 of them holding 6-10, 3-6 and 0-3 m rings round an island and the rest islets. Every
    zoom from 8 to 12 keeps the shallows and the islands (the overview cuts zooms 8-9,
    LEVELS[0] zooms 10-11)."""
    bx, by = 1162, 595  # south of Porkkala, in Finnish waters
    rows, holes, spots = [], [], []
    m = vchart.UNITS * vchart.TILE_BUFFER // vchart.EXTENT
    for i in range(5):
        for j in range(5):
            cx, cy = 6554 + i * 13107, 6554 + j * 13107
            r = 1500 + 1000 * ((i * 7 + j * 3) % 5) / 4
            edges = [_ring(cx, cy, r + d, phase=i + j) for d in (0, 1200, 2400, 3600)]
            for (d1, d2), outer, inner in (((0, 3), 1, 0), ((3, 6), 2, 1), ((6, 10), 3, 2)):
                rows.append([[d1, d2], None, [edges[outer], edges[inner]]])
            holes.append(edges[3])
            f = 1 + 0.15 * math.sin(i + j)  # the rings' wobble at angle 0
            spots.append([(cx, cy, None), *((cx + f * (r + d), cy, band) for d, band in ((600, 1), (1800, 2), (3000, 3)))])
    for i in range(32):  # islets between them
        for j in range(32):
            cx, cy = 1024 + i * 2048, 1024 + j * 2048
            if all(math.dist((cx, cy), (6554 + a * 13107, 6554 + b * 13107)) > 6400 for a in range(5) for b in range(5)):
                holes.append(_ring(cx, cy, 200, n=12))
    assert len(holes) > 100
    box = [(-m, -m), (vchart.UNITS + m, -m), (vchart.UNITS + m, vchart.UNITS + m), (-m, vchart.UNITS + m)]
    rows.append([[10, 20], None, [box, *holes]])
    full = vchart.layers_from_rows({"depth": rows})
    levels, overview = vchart.make_levels(full), vchart.make_overview(full)
    for z in (8, 9, 10, 11, 12):
        data = overview if z < vchart.BLOCK_MIN_Z else levels[vchart.level_for(z)]
        if z >= vchart.BLOCK_Z:
            n = 2 ** (z - vchart.BLOCK_Z)
            tx, ty, ox, oy, k = bx * n, by * n, 0, 0, vchart.EXTENT * n / vchart.UNITS
        else:
            n = 2 ** (vchart.BLOCK_Z - z)
            tx, ty = bx // n, by // n
            ox, oy, k = (bx - tx * n) * vchart.UNITS, (by - ty * n) * vchart.UNITS, vchart.EXTENT / (vchart.UNITS * n)
        tile = vchart.make_tile(z, tx, ty, {(bx, by): data})
        assert tile["land"]
        land = right = total = 0
        wrong = []
        for island in spots:
            for x, y, band in island:
                px, py = (x + ox) * k, (y + oy) * k
                if not (0 <= px < vchart.EXTENT and 0 <= py < vchart.EXTENT):
                    continue
                if z == 8 and band is not None:
                    continue  # 60-75 m bands, under a pixel at zoom 8: only the islands there
                covered = _covered(tile["depth"], px, py)
                total += 1
                wrong.append((band, [row[0] for row in covered]))
                if band is None:
                    land += not covered and bool(_covered([[r] for r in tile["land"]], px, py))
                    right += not covered
                else:
                    right += [row[0] for row in covered][-1:] == [band]
        from collections import Counter
        bad = Counter((b, tuple(c)) for b, c in wrong if (c[-1:] != [b] if b is not None else bool(c)))
        assert total and right / total >= 0.9, (z, right, total, bad)
        assert land >= 0.9 * total / (1 if z == 8 else 4), (z, land, total)


def test_files_with_drawn_contours_are_fetched_again(tmp_path):
    # 0.16.5 drew contours along depth-area edges and marked the files "fill".
    assert vchart._fetch_again({"missing": [], "fill": 1}, "1_2.fwc")
    chart = VectorChart(tmp_path, None, fake_chart_layer)

    async def run():
        z, x, y = tile_at(60.005, 24.4, 12)
        await chart.tile(z, x, y)
        key = vchart.blocks_for(z, x, y)[0]
        path = chart._path(*key)
        assert "fill" not in vchart.read_meta(path)
        assert not await chart._gap_filled(path)
        head, full = vchart.read_block(path, len(vchart.LEVELS) - 1)
        vchart.write_block(path, {**{k: v for k, v in head.items() if k != "ov"}, "fill": 1},
                           vchart.make_levels(full), vchart.make_overview(full))
        os.utime(path, (1000.0, time.time() - 2 * vchart.FAILED_RETRY))
        chart._filled.clear()
        chart._forget()
        assert await chart._gap_filled(path)
        await chart._ensure_part(*key, "")
        assert "fill" not in vchart.read_meta(path)
        assert time.time() - path.stat().st_mtime < 60  # fetched again
        assert not await chart._gap_filled(path)

    asyncio.run(run())


def test_lake_names_on_islands_wait_for_close_zooms():
    # An island (a hole in the depth area) with a lake on it: the lake's name doesn't take the
    # island's place until zoomed right in.
    island = [(20000, 20000), (40000, 20000), (40000, 40000), (20000, 40000)]
    box = [(0, 0), (65536, 0), (65536, 65536), (0, 65536)]
    rows = {"depth": [[["10", "20"], None, [box, island]]],
            "name": [[["Träsket", "swe", 36200], 30000.0, 28000.0],
                     [["Träskön eller Storlandet", "swe", None], 30000.0, 32000.0],
                     [["Kyrkfjärden", "swe", 36490], 25000.0, 18000.0]]}
    layers = vchart.layers_from_rows(rows)

    def names(z):
        n = 2 ** (z - vchart.BLOCK_Z)  # the tile with the lake in it
        tile = vchart.make_tile(z, n * 1162 + 30000 * n // 65536, n * 595 + 28000 * n // 65536, {(1162, 595): layers})
        return sorted(r[2] for r in tile.get("name", []))

    assert names(13) == ["Kyrkfjärden", "Träskön eller Storlandet"]
    assert "Träsket" in names(15)


def test_leading_line_solid_only_along_the_fairway(tmp_path):
    # The simulator's Dragesvik leading line runs from its rear mark past the front mark
    # out to sea in the south, and its boat route joins it 1.4 km south of the front mark;
    # a directional light on Svartö has a narrow white beam.
    async def run():
        chart = VectorChart(tmp_path, None, fake_chart_layer, mml_key="test")
        lead_tile, _ = await chart.tile(*tile_at(59.9876, 24.0918, 14))
        dir_tile, _ = await chart.tile(*tile_at(59.9982, 24.1756, 14))
        return lead_tile, dir_tile

    lead_tile, dir_tile = asyncio.run(run())
    # Dashed by the marks, solid only from where the route joins it on out to sea.
    (bearing, line, solid), = lead_tile["lead"]
    front = next(a for a in lead_tile["aid"] if a[5] == "Dragesvik alempi")
    assert solid and all(len(piece) >= 4 for piece in solid)
    assert min(min(piece[1::2]) for piece in solid) > front[1] + 150
    assert abs(front[7] - (bearing + 180) % 360) < 0.5  # the mark's bearing out to sea
    assert all(len(a) == 7 or a[7] is None for a in lead_tile["aid"] if a[5] != "Dragesvik alempi")
    light = next(a for a in dir_tile["aid"] if a[2] == "d")
    assert light[5] == "Svartö suunta" and len(light) == 7  # not on a leading line, not leaning
    assert sorted(s[4] for s in dir_tile["sector"] if s[:2] == light[:2]) == ["p", "v", "vi"]


def test_spar_buoys_lean_clear_of_each_other():
    from ferrywatch.vchart import LayerBuilder, spar_leans
    aids = LayerBuilder("point")
    aids.point(("Viitta", 1, "E", "lone"), 10000, 10000)
    aids.point(("Viitta", 1, "E", "left"), 30000, 10000)
    aids.point(("Viitta", 2, "E", "right"), 30150, 10000)
    aids.point(("Poiju", 1, "E", "buoy"), 50000, 10000)
    fline = LayerBuilder("line")
    fline.line((None, None), [(50350, 8000), (50350, 10600)])  # just right of the buoy
    leans = spar_leans({"aid": aids.layer, "fline": fline.layer})
    assert leans[(10000, 10000)] == 0  # nothing in its way: upright
    assert leans[(30000, 10000)] < 0 < leans[(30150, 10000)]  # side by side: apart
    assert all(abs(a) <= 50 for a in leans.values())
    assert leans[(50000, 10000)] <= 10  # not its usual lean to the right, over the fairway


def _big_depth_answer():
    """A Traficom depth area as the NAS got it around the Archipelago Sea: one MultiPolygon
    whose island holes reach far outside the block asked for."""
    def square(x, y, r):
        return [[x - r, y - r], [x + r, y - r], [x + r, y + r], [x - r, y + r], [x - r, y - r]]
    holes_in = [square(1000.5 + i * 10, 1000.25, 2) for i in range(5)]
    holes_out = [square(50000 + i * 10, 50000, 2) for i in range(200)]
    far_poly = [square(90000, 90000, 100)]
    feature = {"type": "Feature", "id": "DepthArea_A.1",
               "geometry": {"type": "MultiPolygon",
                            "coordinates": [[square(0, 0, 100000), *holes_in, *holes_out], far_poly]},
               "geometry_name": "geom",
               "properties": {"DRVAL1": 10.0, "DRVAL2": 20.0, "OBJNAM": "[not] coordinates"}}
    return {"type": "FeatureCollection", "features": [feature], "numberMatched": 1, "numberReturned": 1}


def test_lean_geojson_keeps_only_what_reaches_the_block():
    body = json.dumps(_big_depth_answer()).encode()
    for size in (1, 7, 64, len(body)):  # any chunking, even mid-number or mid-key
        lean = vchart.LeanGeoJSON((0, 0, 2000, 2000))
        for i in range(0, len(body), size):
            lean.feed(body[i:i + size])
        page = lean.close()
        (feature,) = page["features"]
        assert feature["properties"] == {"DRVAL1": 10.0, "DRVAL2": 20.0, "OBJNAM": "[not] coordinates"}
        (poly,) = feature["geometry"]["coordinates"]  # the far polygon dropped whole
        assert len(poly) == 6 and poly[1][0] == [998.5, 998.25]  # outer ring and the 5 holes inside
        assert lean.kept == 6 and lean.dropped == 201


def test_page_too_big_for_one_feature_is_read_lean(tmp_path, monkeypatch):
    asked = []

    async def once(self, url, params, what):
        asked.append(int(params["count"]))
        raise vchart._TooBig("too big")

    async def lean(self, url, params, what):
        asked.append(("lean", params["count"]))
        return _big_depth_answer()

    monkeypatch.setattr(VectorChart, "_get_once", once)
    monkeypatch.setattr(VectorChart, "_get_lean", lean)
    chart = VectorChart(tmp_path, None)
    params = {"bbox": "0,0,2000,2000,EPSG:3857"}
    features = asyncio.run(chart._paged("https://example.org", params, "depth", None))
    assert len(features) == 1
    assert asked[0] == vchart.PAGE and asked[-2:] == [1, ("lean", "1")]  # halved all the way down first


def test_unreachable_service_is_left_alone_longer_each_time(tmp_path, monkeypatch):
    calls = []

    async def once(self, url, params, what):
        calls.append(url)
        raise asyncio.TimeoutError()

    monkeypatch.setattr(VectorChart, "_get_once", once)
    chart = VectorChart(tmp_path, None)
    try:
        asyncio.run(chart._get_bytes("https://example.org", None, "lake"))
    except asyncio.TimeoutError:
        pass
    assert len(calls) == 1  # no connection at all: not asked again at once
    now = time.time()
    pauses = []
    for _ in range(7):
        chart._service_down(vchart.MML_API)
        pauses.append(round(chart._down[vchart.MML_API] - now, -1))
    assert pauses[:3] == [vchart.SERVICE_DOWN_FOR, 2 * vchart.SERVICE_DOWN_FOR, 4 * vchart.SERVICE_DOWN_FOR]
    assert pauses[-1] == vchart.SERVICE_DOWN_MAX


def test_busy_answer_is_asked_again(tmp_path, monkeypatch):
    monkeypatch.setattr(vchart, "GET_RETRIES", (0, 0))
    calls = []

    async def once(self, url, params, what):
        calls.append(url)
        if len(calls) < 2:
            raise vchart._HTTPError(403, "<html><head><title>403 Forbidden</title></head></html>")
        return bytearray(b"{}")

    monkeypatch.setattr(VectorChart, "_get_once", once)
    chart = VectorChart(tmp_path, None)
    assert asyncio.run(chart._get_bytes("https://example.org", None, "sign")) == bytearray(b"{}")
    assert len(calls) == 2


def test_tinted_lanes_are_cut_at_their_block_edge():
    # A lane over two blocks: each block keeps it a margin past its edge. Zoomed out, where
    # one tile holds both blocks, the two copies must meet at the edge and not overlap.
    m = vchart.UNITS * vchart.TILE_BUFFER // vchart.EXTENT
    u = vchart.UNITS

    def block(x0, x1, kind):
        ring = [[x0, 20000], [x1, 20000], [x1, 40000], [x0, 40000]]
        return vchart.layers_from_rows({"tss": [[[kind, 90], [x0, 20000, x1, 40000], [ring]]]})

    def area(flat):
        pts = list(zip(flat[::2], flat[1::2]))
        return abs(sum(x0 * y1 - x1 * y0 for (x0, y0), (x1, y1) in zip(pts, pts[1:] + pts[:1]))) / 2

    for kind in ("lane", "zone"):
        tile = vchart.make_tile(10, 0, 0, {(0, 0): block(30000, u + m, kind), (1, 0): block(-m, 30000, kind)})
        rows = [r for r in tile["tss"] if r[0] == kind]
        assert len(rows) == 2
        xs = sorted(round(min(r[2][::2]), 1) for r in rows) + sorted(round(max(r[2][::2]), 1) for r in rows)
        assert 256.0 in xs[:2] and 256.0 in xs[2:]  # one piece ends, the other starts, at the edge
        k = vchart.EXTENT / (2 * u)
        assert abs(sum(area(r[2]) for r in rows) - u * k * 20000 * k) < 50  # an overlap would add ~1900
    # Outlined areas keep their margin (drawing a line twice doesn't darken it).
    tile = vchart.make_tile(10, 0, 0, {(0, 0): block(30000, u + m, "prec"), (1, 0): block(-m, 30000, "prec")})
    assert max(max(r[2][::2]) for r in tile["tss"] if min(r[2][::2]) < 200) > 256


def test_land_heights_in_millimetres():
    # The Land Survey's contour heights are millimetres; metres stay as they are.
    assert vchart._height_m(5000) == 5 and vchart._height_m(2500) == 2.5 and vchart._height_m(20) == 20
    tile = {}
    vchart._format(tile, {"height": [[(20000,), [0, 0, 5, 5]], [(2500,), [0, 0, 5, 5]]]}, 15, {})
    assert [m for m, _ in tile["height"]] == [20, 2.5]


def test_buildings_true_size():
    _, bx, by = tile_at(60.1, 24.4, 11)
    x0, y0, x1, y1 = block_bounds(bx, by)
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    house = {"geometry": {"type": "Polygon", "coordinates": [[[cx, cy], [cx + 24, cy], [cx + 24, cy + 20],
                                                              [cx, cy + 20], [cx, cy]]]},
             "properties": {"kohdeluokka": 42211}}
    shed = {"geometry": {"type": "Polygon", "coordinates": [[[cx + 50, cy], [cx + 56, cy], [cx + 56, cy + 6],
                                                             [cx + 50, cy + 6], [cx + 50, cy]]]},
            "properties": {"kohdeluokka": 42211}}
    points, outlines = vchart.LayerBuilder("point"), vchart.LayerBuilder("area")
    vchart.build_into(points, bx, by, "building", [house, shed], extra=outlines)
    # About 12 x 10 m on the ground at 60°N: a 11 m square's area, and its outline; the shed has no outline.
    assert [p[2] for p in points.layer.props] == [11, 3] and outlines.layer.n == 1
    rows = {"building": [[10, 10, points.layer.props[0]], [30, 10, points.layer.props[1]]]}
    z14, z13 = {}, {}
    vchart._format(z14, rows, 14, {})
    vchart._format(z13, rows, 13, {})
    assert z14["building"] == [[30, 10, 1, 3]]  # the house by its outline
    assert z13["building"] == [[10, 10, 1, 11], [30, 10, 1, 3]]
    # Files from before 0.24 have no side: drawn by size class.
    old = {}
    vchart._format(old, {"building": [[1, 2, (42211, 1)]]}, 14, {})
    assert old["building"] == [[1, 2, 1, None]]
