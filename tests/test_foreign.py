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
    assert t12["land"] and {row[0] for row in t12["depth"]} >= {1, 4}
    assert any(a[5] == "Naissaar" and a[2] == "m" for a in t12["aid"])
    assert any(text.startswith("Fl(2) W 12s") for _, _, text in t12["light"])
    assert t12["contour"]
    # The overview has foreign depths and land as well as the Finnish ones.
    assert t8["land"] and t8["depth"] and t8["contour"]
    assert [row[0] for row in t8["depth"]] == sorted((row[0] for row in t8["depth"]), reverse=True)
    # Where the two meet both sides have water: deep on both.
    assert {row[0] for row in seam["depth"]} == {4}
    # Nothing foreign inside Finnish waters: no foreign sea mark north of the border.
    for x, y, *_ in seam.get("aid", ()):
        assert y > 200


def test_band_rings_drop_shared_box_edges():
    box = (0, 0, 10, 10)
    outer = [[(0, 0), (10, 0), (10, 10), (0, 10)]]  # the whole box is deeper than 3 m...
    holes = [[(0, 0), (10, 0), (10, 4), (0, 4)]]  # ...and its top part deeper than 6 m
    rings = F.band_rings(outer, holes, box)
    assert len(rings) == 1 and abs(F._area(rings[0]) - 60) < 1e-9
    # No edge of the result runs along the box where the hole was (y from 0 to 4 on the sides).
    for ring in rings:
        for (ax, ay), (bx, by) in zip(ring, ring[1:] + ring[:1]):
            assert not (ax == bx and ax in (0, 10) and max(ay, by) <= 4)
    island = [[(4, 4), (6, 4), (6, 6), (4, 6)]]
    assert len(F.band_rings(outer, island, box)) == 2  # a hole away from the edge stays a hole
