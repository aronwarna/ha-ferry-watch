import asyncio
import math

from ferrywatch import foreign as F
from ferrywatch import vchart as V
from ferrywatch.simforeign import geotiff, lzw_encode
from ferrywatch.simulate import fake_chart_layer


def tile_at(lat, lon, z):
    n = 2**z
    return z, int((lon + 180) / 360 * n), int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n)


def test_geotiff_parsed_as_depths():
    # 3 x 2 cells of elevation, one without data.
    data = geotiff(3, 2, 24.0, 60.0, 0.1, 0.05, [-10, -20, 5, -99999, -40, -50])
    g = F.parse_geotiff(data)
    assert (g.w, g.h, g.lon0, g.lat0) == (3, 2, 24.0, 60.0)
    assert g.depth(24.05, 59.975) == 10.0  # the first cell's centre
    assert g.depth(24.25, 59.925) == 50.0
    assert g.depth(24.05, 59.925) == 40.0  # nearest with data next to the empty cell
    assert g.depth(23.5, 59.9) is None
    assert F.Grid.from_bytes(g.to_bytes()).values == g.values


def test_geotiff_compressed():
    values = [-(k % 37) - 0.5 for k in range(600)]
    for compression in (5, 8):
        g = F.parse_geotiff(geotiff(30, 20, 0.0, 2.0, 0.1, 0.1, values, compression=compression))
        assert list(g.values) == [round(v * 10) for v in values]
    assert F._lzw_decode(lzw_encode(bytes(range(256)) * 40)) == bytes(range(256)) * 40
    g = F.parse_geotiff(geotiff(30, 20, 0.0, 2.0, 0.1, 0.1, values, compression=8, predictor=3))
    assert list(g.values) == [round(v * 10) for v in values]


def test_march_rings_and_contours():
    n = 41
    rows = [[10 - math.hypot(i - 20, j - 20) / 2 for i in range(n)] for j in range(n)]
    rings = F.march(rows, 5.0, True)
    assert len(rings) == 1
    area = F._area(rings[0])
    assert abs(area - math.pi * 10 ** 2) / (math.pi * 100) < 0.05
    lines = F.march(rows, 5.0, False)
    assert len(lines) == 1 and lines[0][0] == lines[0][-1]
    # A level above everything: nothing.
    assert F.march(rows, 50.0, True) == []


def test_coastline_side_decides_land():
    n = 40
    depth = __import__("array").array("d", [20.0] * (n * n))  # the model says all water
    coast = F.CoastGrid(n)
    # An island, counter-clockwise in lon/lat, i.e. clockwise with y down: land on the left.
    pts = [(20 + 8 * math.cos(a), 20 - 8 * math.sin(a)) for a in (2 * math.pi * k / 60 for k in range(61))]
    coast.add(pts)
    vals = F.classify(n, depth, coast)
    assert vals[20 * n + 20] == F.LAND  # inside the island
    assert vals[2 * n + 2] == 20.0  # open water keeps the model's depth
    # Next to the line the value is twice the signed distance, so the shore follows the line.
    assert abs(vals[20 * n + 28]) < 0.1 and 1.5 < vals[20 * n + 29] <= 2 and -2 <= vals[20 * n + 27] < -1.5


def test_light_character_and_aids():
    tags = {"seamark:type": "light_minor", "seamark:light:1:colour": "white", "seamark:light:1:sector_start": "10",
            "seamark:light:1:sector_end": "20", "seamark:light:1:character": "Fl", "seamark:light:1:group": "2",
            "seamark:light:1:period": "10", "seamark:light:2:colour": "red", "seamark:light:2:sector_start": "20",
            "seamark:light:2:sector_end": "80", "seamark:light:2:character": "Fl", "seamark:name": "Ö"}
    rows, text = F.light_rows(tags)
    assert rows == [(10.0, 20.0, "v"), (20.0, 80.0, "p")] and text == "Fl(2) WR 10s"
    assert F.aid_props(tags) == ("sektoriloisto", 99, "K", "Ö")
    assert F.aid_props({"seamark:type": "buoy_lateral", "seamark:buoy_lateral:category": "port",
                        "seamark:buoy_lateral:shape": "spar"}) == ("viitta", 1, "E", None)
    assert F.aid_props({"seamark:type": "buoy_cardinal", "seamark:buoy_cardinal:category": "west"})[:2] == ("poiju", 5)
    assert F.aid_props({"seamark:type": "mooring"}) is None
    rows, text = F.light_rows({"seamark:light:colour": "green", "seamark:light:character": "Q"})
    assert rows == [(0, 360, "vi")] and text == "Q G"


def test_osm_stream_parses_elements():
    got = []
    s = F.OsmStream(got.append)
    xml = (b'<osm><node id="1" lat="59.5" lon="24.5"><tag k="seamark:type" v="rock"/></node>'
           b'<way id="2"><nd ref="1" lat="59.5" lon="24.5"/><nd ref="3" lat="59.6" lon="24.6"/></way>'
           b'<relation id="4"><member type="way" ref="2" role="outer"><nd lat="1" lon="2"/></member>'
           b'<tag k="seamark:type" v="separation_zone"/></relation></osm>')
    for k in range(0, len(xml), 7):  # in arbitrary pieces, as it arrives
        s.feed(xml[k:k + 7])
    s.close()
    assert got[0] == {"type": "node", "tags": {"seamark:type": "rock"}, "lat": 59.5, "lon": 24.5}
    assert got[1] == {"type": "way", "tags": {}, "geometry": [(24.5, 59.5), (24.6, 59.6)]}
    assert got[2]["members"] == [{"role": "outer", "geometry": [(2.0, 1.0)]}]


def test_clip_convex_and_merge():
    square = [(0, 0), (10, 0), (10, 10), (0, 10)]
    tri = [(5, -5), (15, 5), (5, 15)]
    part = F.clip_convex(square, tri)
    assert abs(F._area(part) - 50) < 1e-6
    assert F.clip_line_convex([(0, 5), (20, 5)], tri) == [[(5.0, 5.0), (15.0, 5.0)]]
    a = V.layers_from_rows({"depth": [[[0, 3], None, [[(0, 0), (5, 0), (5, 5)]]]]})["depth"]
    b = V.layers_from_rows({"depth": [[[3, 6], None, [[(1, 1), (2, 1), (2, 2)], [(3, 3), (4, 3), (4, 4)]]]]})["depth"]
    m = F.merge_layers(a, b)
    assert m.n == 2 and m.props == [(0, 3), (3, 6)]
    assert [len(list(m.rings(i))) for i in range(2)] == [1, 2]
    assert m.pts(m.vo[m.rs[1]], m.vo[m.rs[1] + 1]) == [(1, 1), (2, 1), (2, 2)]


def test_foreign_area_is_outside_finland():
    assert F.in_finland_m(*V.lonlat_to_mercator(24.4, 60.0))
    assert not F.in_finland_m(*V.lonlat_to_mercator(24.75, 59.5))
    assert F.wanted(V.block_bounds(*tile_at(59.5, 24.75, 11)[1:]))
    assert not F.wanted(V.block_bounds(*tile_at(60.1, 24.4, 11)[1:]))  # Porkkala
    assert not F.wanted(V.block_bounds(*tile_at(55.0, 12.0, 11)[1:]))  # the Sound: not drawn


def test_tiles_outside_finland(tmp_path):
    chart = V.VectorChart(tmp_path, None, fake_chart_layer)

    async def run():
        t12, ok = await chart.tile(*tile_at(59.60, 24.53, 12))  # Naissaar, off Tallinn
        t8, ok8 = await chart.tile(*tile_at(59.7, 24.9, 8))
        seam, _ = await chart.tile(*tile_at(59.72, 24.4, 11))  # on the Finnish sea border
        return t12, ok, t8, ok8, seam

    t12, ok, t8, ok8, seam = asyncio.run(run())
    assert ok  # (the overview lacks Finnish blocks not fetched yet)
    assert t12["land"] and {row[0] for row in t12["xdepth"]} >= {1, 4}
    assert [row[0] for row in t12["xdepth"]] == sorted(row[0] for row in t12["xdepth"])  # shallow first
    assert any(a[5] == "Naissaar" and a[2] == "m" for a in t12["aid"])
    assert any(text.startswith("Fl(2) W 12s") for _, _, text in t12["light"])
    assert t12["contour"]
    # The overview has foreign depths and land as well as the Finnish ones.
    assert t8["land"] and t8["xdepth"] and t8["contour"]
    assert [row[0] for row in t8["xdepth"]] == sorted(row[0] for row in t8["xdepth"])
    # Where the two meet both sides have water: deep on both.
    assert {row[0] for row in seam.get("depth", ())} <= {4} and max(row[0] for row in seam["xdepth"]) == 4
    # Nothing foreign inside Finnish waters: no foreign sea mark north of the border.
    for x, y, *_ in seam.get("aid", ()):
        assert y > 200


def test_foreign_side_can_be_turned_off(tmp_path):
    from ferrywatch.simulate import fake_chart_layer
    from ferrywatch.vchart import FOREIGN_PART, VectorChart
    chart = VectorChart(tmp_path, None, fake_chart_layer)
    assert chart.foreign.enabled and FOREIGN_PART in chart._parts()
    rev = chart._rev()
    chart.set_foreign(False)
    assert not chart.foreign.enabled and FOREIGN_PART not in chart._parts()
    assert chart._rev() != rev and chart.status()["foreign"] is False
    chart.set_foreign(True)
    assert chart._rev() == rev


def test_geotiff_without_georeference_uses_the_area_asked_for():
    import struct
    w, h = 4, 2
    vals = [-10.0] * (w * h)
    # A bare little-endian float32 TIFF: no GeoTIFF tags at all.
    pix = struct.pack("<%df" % len(vals), *vals)
    entries = [(256, 3, 1, w), (257, 3, 1, h), (258, 3, 1, 32), (259, 3, 1, 1), (273, 4, 1, 8),
               (277, 3, 1, 1), (278, 3, 1, h), (279, 4, 1, len(pix)), (339, 3, 1, 3)]
    ifd_off = 8 + len(pix)
    ifd = struct.pack("<H", len(entries)) + b"".join(
        struct.pack("<HHI", t, typ, n) + (struct.pack("<HH", v, 0) if typ == 3 else struct.pack("<I", v))
        for t, typ, n, v in entries) + struct.pack("<I", 0)
    data = b"II*\x00" + struct.pack("<I", ifd_off) + pix + ifd
    import pytest
    with pytest.raises(F.TiffError):
        F.parse_geotiff(data)
    g = F.parse_geotiff(data, (24.0, 59.0, 24.4, 59.2))
    assert g is not None


def test_emodnet_geotiff_with_model_transformation():
    """As ben's sample from EMODnet (off Tallinn): big-endian float32 in one tile, 480 x 288,
    no PixelScale or Tiepoint but a ModelTransformation, a GeoKeyDirectory and no NoData."""
    import struct
    w, h, cell, lon0, lat0 = 480, 288, 1 / 960, 24.4, 59.7
    vals = []
    for j in range(h):
        lat = lat0 - (j + 0.5) * cell
        for i in range(w):
            lon = lon0 + (i + 0.5) * cell
            # Naissaar as a hill of 20 m, the sea 30 m deep round it.
            island = math.hypot((lon - 24.53) * 0.5, lat - 59.575) < 0.02
            vals.append(20.0 if island else -30.0)
    pix = struct.pack(">%df" % len(vals), *vals)
    matrix = struct.pack(">16d", cell, 0, 0, lon0, 0, -cell, 0, lat0, 0, 0, 0, 0, 0, 0, 0, 1)
    geokeys = struct.pack(">16H", 1, 1, 0, 3, 1024, 0, 1, 2, 1025, 0, 1, 1, 2048, 0, 1, 4326)
    rational = struct.pack(">II", 1, 1)
    # tag, type, count, inline value or bytes stored after the IFD
    entries = [(256, 3, 1, w), (257, 3, 1, h), (258, 3, 1, 32), (259, 3, 1, 1), (262, 3, 1, 1), (277, 3, 1, 1),
               (282, 5, 1, rational), (283, 5, 1, rational), (296, 3, 1, 1), (322, 3, 1, w), (323, 3, 1, h),
               (324, 4, 1, None), (325, 4, 1, len(pix)), (339, 3, 1, 3), (34264, 12, 16, matrix),
               (34735, 3, 16, geokeys)]
    ifd_size = 2 + 12 * len(entries) + 4
    extra = b"".join(v for *_, v in entries if isinstance(v, bytes))
    data_off = 8 + ifd_size + len(extra)
    ifd, at = struct.pack(">H", len(entries)), 8 + ifd_size
    for tag, typ, n, v in entries:
        if isinstance(v, bytes):
            ifd += struct.pack(">HHII", tag, typ, n, at)
            at += len(v)
        elif typ == 3:
            ifd += struct.pack(">HHIHH", tag, typ, n, v, 0)
        else:
            ifd += struct.pack(">HHII", tag, typ, n, data_off if v is None else v)
    data = b"MM\x00*" + struct.pack(">I", 8) + ifd + struct.pack(">I", 0) + extra + pix
    g = F.parse_geotiff(data)
    assert (g.w, g.h) == (w, h)
    assert abs(g.lon0 - lon0) < 1e-9 and abs(g.lat0 - lat0) < 1e-9
    assert abs(g.dlon - 0.0010417) < 1e-6 and abs(g.dlat - 0.0010417) < 1e-6
    assert g.depth(24.53, 59.575) == -20.0  # Naissaar: land
    assert g.depth(24.75, 59.65) == 30.0  # open water


def test_smoothing_takes_out_noise_but_keeps_land():
    from array import array
    n = 20
    depth = array("d", [8.0 + (4 if (i * 7 + j * 3) % 5 == 0 else -2) for j in range(n) for i in range(n)])
    for k in range(n):
        depth[k] = -1.0  # a strip of land along the top
    depth[5 * n + 5] = math.nan
    before = max(depth[2 * n:]) - min(d for d in depth[2 * n:] if d == d)
    F.smooth(n, depth, 2)
    assert all(depth[k] == -1.0 for k in range(n))  # land stays land
    assert math.isnan(depth[5 * n + 5])
    mid = [depth[j * n + i] for j in range(6, 14) for i in range(6, 14)]
    assert max(mid) - min(mid) < before / 3
    assert depth[n + 5] < depth[10 * n + 5]  # next to the land the mean errs shallow


def test_foreign_depths_nested_without_specks():
    """Noisy model depths round the 3, 6 and 10 m levels: the block's depth areas are nested
    levels with no ring smaller than MIN_RING cells, and the tile paints them shallow first."""
    class Noisy:
        def depth_m(self, mx, my, lon, lat):
            i, j = int(lon * 960), int(lat * 960)
            noise = ((i * 73856093 ^ j * 19349663) & 0xFFFF) / 0x7FFF - 1
            return 6.0 + 5 * noise + 300 * max(0.0, lat - 59.52)

    z, bx, by = tile_at(59.52, 24.6, 11)
    b = F.BlockBuilder(bx, by, Noisy())
    built = b.finish(True)
    assert "depth" not in built
    xdepth = built["xdepth"]
    levels = [p[0] for p in xdepth.props]
    assert levels == [0, 3, 6, 10]
    cell2 = b.cell ** 2
    for i in range(xdepth.n):
        if xdepth.props[xdepth.pi[i]][0] == 0:
            continue
        for r in xdepth.rings(i):
            ring = xdepth.pts(xdepth.vo[r], xdepth.vo[r + 1])
            x0, y0, x1, y1 = V._bbox(ring)
            on_edge = x0 <= b.box[0] or y0 <= b.box[1] or x1 >= b.box[2] or y1 >= b.box[3]
            assert on_edge or F._area(ring) >= F.MIN_RING * cell2 * 0.5  # (simplified a little)
    tile = V.make_tile(12, 2 * bx, 2 * by, {(bx, by): V.make_levels(built)[-1]})
    assert [row[0] for row in tile["xdepth"]] == [1, 2, 3, 4]


def test_tiny_foreign_rings_left_out_zoomed_out():
    big = [(1000, 1000), (60000, 1000), (60000, 60000), (1000, 60000)]
    islet = [(30000, 30000), (30060, 30000), (30060, 30060), (30000, 30060)]  # about 9 m across
    b = V.LayerBuilder("area")
    b.area((0,), [big, islet])
    layers = {"xdepth": b.layer}
    t10 = V.make_tile(10, 0, 0, {(0, 0): layers, (1, 0): None, (0, 1): None, (1, 1): None})
    t15 = V.make_tile(15, 7, 7, {(0, 0): layers})
    assert len(t10["xdepth"][0]) == 2  # the band and the big ring only
    assert len(t15["xdepth"][0]) == 3  # up close the islet is there


def test_osm_file_keeps_elements_and_filters_coast(tmp_path):
    path = tmp_path / "osm.bin.gz"
    f = F.OsmFile(path)
    f.add({"type": "way", "tags": {}, "geometry": [(18.0, 59.0), (18.1, 59.05)]})
    f.add({"type": "way", "tags": {}, "geometry": [(19.5, 60.0), (19.6, 60.1)]})
    mark = {"type": "node", "lat": 59.02, "lon": 18.05, "tags": {"seamark:type": "rock"}}
    f.add(mark)
    f.close()
    got = list(F.OsmFile.read(path, (17.9, 58.9, 18.2, 59.1)))
    assert len(got) == 2 and got[1] == mark
    assert got[0]["geometry"][1] == (18.1, 59.05)


def test_one_overpass_request_per_group_of_blocks(tmp_path):
    chart = V.VectorChart(tmp_path, None, fake_chart_layer)
    _, bx, by = tile_at(59.58, 24.62, 11)
    bx, by = bx - bx % 2, by - by % 2

    async def run():
        for key in ((bx, by), (bx + 1, by), (bx, by + 1), (bx + 1, by + 1)):
            chart._wanted[key] = __import__("time").monotonic()
        await asyncio.gather(*(chart.foreign.fetch_part(x, y) for x, y in
                               ((bx, by), (bx + 1, by), (bx, by + 1), (bx + 1, by + 1))))
        first = chart.foreign.overpass_requests
        chart._wanted[(bx + 2, by)] = __import__("time").monotonic()
        await chart.foreign.fetch_part(bx + 2, by)
        return first, chart.foreign.overpass_requests

    first, after = asyncio.run(run())
    assert first == 1 and after == 2


def test_depth_grids_are_let_go_after_the_builds(tmp_path, monkeypatch):
    monkeypatch.setattr(F, "GRID_KEEP", 0.05)
    chart = V.VectorChart(tmp_path, None, fake_chart_layer)
    _, bx, by = tile_at(59.58, 24.62, 11)

    async def run():
        chart._wanted[(bx, by)] = __import__("time").monotonic()
        assert await chart.foreign.fetch_part(bx, by)
        held = chart.foreign.grid_bytes()
        await asyncio.sleep(0.2)
        return held, chart.foreign.grid_bytes()

    held, after = asyncio.run(run())
    assert held > 0 and after == 0


def test_blocks_nearest_the_view_are_built_first():
    import time as _time

    async def run():
        now = _time.monotonic()
        wanted = {(10, 10): now, (11, 10): now, (50, 50): math.inf}
        slot = F.NearestFirst(wanted)
        order = []

        async def build(key):
            await slot.acquire(key)
            order.append(key)
            await asyncio.sleep(0)
            slot.release()

        await slot.acquire((0, 0))  # busy: the rest queue
        tasks = [asyncio.create_task(build(k)) for k in ((50, 50), (30, 30), (11, 10), (10, 12))]
        await asyncio.sleep(0)
        slot.release()
        await asyncio.gather(*tasks)
        return order

    # Nearest the middle of the blocks just asked for first, the background block last.
    assert asyncio.run(run()) == [(11, 10), (10, 12), (30, 30), (50, 50)]


def test_grid_rows_sample_as_single_points():
    import random
    from array import array
    w, h = 30, 20
    vals = array("h", [random.Random(k).randint(-300, 900) for k in range(w * h)])
    for k in (5, 47, 48, 333):
        vals[k] = F.Grid.NODATA
    g = F.Grid(w, h, 24.0, 60.0, 0.01, 0.005, vals)
    lons = [23.99 + 0.0037 * i for i in range(90)]
    cols = g.columns(lons)
    for lat in (60.002, 59.97, 59.951, 59.9, 59.85):
        row = g.row(cols, lat)
        for lon, got in zip(lons, row):
            want = g.depth(lon, lat)
            assert (math.isnan(got) and want is None) or abs(got - want) < 1e-9
