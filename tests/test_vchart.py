import asyncio
import json
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
    # Soundings from zoom 12, sparser than at 13; zoomed out only the major lights.
    assert "sounding" in t14 and 0 < len(t12["sounding"]) <= 36 and "sounding" not in t9  # one a 128-unit cell
    assert all(a[2] in vchart.MAJOR_AIDS for a in t9["aid"])
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
        # No key: no Land Survey parts. Each source once a block; heritage sites once for the region.
        per_block = sum(len(vchart.source_names(key)) for part in chart._parts() for key in vchart.PARTS[part])
        assert {p for p in vchart.PARTS if p not in vchart.MML_PARTS} == set(chart._parts())
        assert len(asked) == 2 * per_block + 1
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
    assert path.stat().st_size < 8 * (kept + 6000)  # compressed: under the arrays' 8 bytes a vertex
    assert levels[0] == {}  # nothing of it zoomed out (zoom 11 and below)
    assert all(vchart._multiple(p[0], 10) for p in levels[1]["height"].props)
    assert len(levels[1]["height"].co) < len(built["height"].co)
    assert sum(L.nbytes() for L in built.values()) < 10 * (kept + 6000) + 50_000
    assert vchart.read_overview(path) == {}
