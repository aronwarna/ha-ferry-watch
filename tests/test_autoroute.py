import asyncio

from ferrywatch.autoroute import AutoRouter, block_lines, blocks_between, build_graph, find_route, simplify_path
from ferrywatch.geo import distance_m
from ferrywatch.simulate import fake_chart_layer
from ferrywatch.vchart import BLOCK_Z, HALF, VectorChart, build_block, lonlat_to_mercator

NM = 1852


def merc(*pts):
    """[(lat, lon), ...] -> a line in mercator metres."""
    return [lonlat_to_mercator(lon, lat) for lat, lon in pts]


A, B, C = (59.95, 24.30), (59.95, 24.50), (60.05, 24.50)


def network(shortcut_draught):
    """An L-shaped main fairway A-B-C, a diagonal shortcut A-C of the given design draught,
    a branch ending on the middle of A-B (a T), and a line crossing A-B without a shared vertex."""
    deep = [merc(A, B, C), merc((60.04, 24.36), (59.95, 24.36)), merc((59.88, 24.45), (59.97, 24.45))]
    return deep, merc(A, C), shortcut_draught


def lines_for(draught, shortcut_draught=1.2):
    deep, short, short_draught = network(shortcut_draught)
    return deep + ([short] if short_draught >= draught + 0.3 else [])


def length_m(points):
    return sum(distance_m(*a, *b) for a, b in zip(points, points[1:]))


def test_avoids_a_shortcut_too_shallow_for_the_boat():
    start, end = (59.949, 24.29), (60.051, 24.51)
    deep = find_route(build_graph(lines_for(1.5)), start, end)
    shallow = find_route(build_graph(lines_for(0.5)), start, end)
    # 1.5 m boat: the 1.2 m shortcut is out, so the way goes round by B.
    assert any(distance_m(*p, *B) < 50 for p in deep["points"])
    # 0.5 m boat: the shortcut is deep enough and shorter.
    assert not any(distance_m(*p, *B) < 50 for p in shallow["points"])
    assert shallow["metres"] < deep["metres"]
    assert deep["points"][0] == [59.949, 24.29] and deep["points"][-1] == [60.051, 24.51]
    assert abs(length_m(deep["points"]) - deep["metres"]) < 100


def test_t_junction_and_crossing_join_the_network():
    g = build_graph(lines_for(1.5))
    # From the branch's far end to A: down the branch and west along A-B.
    via_t = find_route(g, (60.041, 24.36), (59.949, 24.29))
    assert "error" not in via_t
    assert any(distance_m(*p, 59.95, 24.36) < 30 for p in via_t["points"])
    # From the crossing line's south end: through the crossing point onto A-B.
    via_x = find_route(g, (59.879, 24.45), C)
    assert any(distance_m(*p, 59.95, 24.45) < 30 for p in via_x["points"])
    assert via_x["metres"] < 1.1 * (distance_m(59.88, 24.45, 59.95, 24.45) + distance_m(59.95, 24.45, *B)
                                    + distance_m(*B, *C))


def test_start_far_from_fairways():
    g = build_graph(lines_for(1.5))
    assert "error" in find_route(g, (59.40, 24.40), C)  # over 10 NM away
    near = find_route(g, (59.88, 24.30), C)  # about 4 NM: allowed, with a warning
    assert "error" not in near and near["warnings"]
    assert "error" in find_route(build_graph([]), A, C)


def test_line_cut_by_a_block_edge_is_joined_again():
    size = 2 * HALF / 2**BLOCK_Z
    lat = 60.0
    x0, y = lonlat_to_mercator(24.3, lat)
    bx = int((x0 + HALF) // size)
    by = int((HALF - y) // size)
    edge = -HALF + (bx + 1) * size  # the east edge of block bx
    line = [(edge - 3000, y), (edge + 3000, y + 200)]
    feature = {"geometry": {"type": "LineString", "coordinates": [list(p) for p in line]},
               "properties": {"mitoitussyvays": 6.0, "jnro": 1}}
    lines = []
    for b in (bx, bx + 1):
        layer = build_block(b, by, {"fline": [feature]})["fline"]
        lines += block_lines(layer, b, by, 1.5)
        assert block_lines(layer, b, by, 6.0) == []  # 6 m design draught < 6.0 + 0.3
    assert len(lines) == 2
    g = build_graph(lines)
    assert g.n == 3  # the two ends and one shared point on the edge
    from ferrywatch.vchart import mercator_to_lonlat
    (lo1, la1), (lo2, la2) = mercator_to_lonlat(*line[0]), mercator_to_lonlat(*line[1])
    route = find_route(g, (la1, lo1), (la2, lo2))
    assert "error" not in route and len(route["points"]) <= 3


def test_simplify_keeps_corners_and_caps_points():
    zigzag = [(60 + i * 0.001, 24 + (i % 2) * 0.0001) for i in range(300)]
    assert len(simplify_path(zigzag, max_points=20)) <= 20
    corner = [(60.0, 24.0), (60.0, 24.1), (60.0, 24.2), (60.1, 24.2)]
    assert simplify_path(corner) == [[60.0, 24.0], [60.0, 24.2], [60.1, 24.2]]


def test_blocks_between_is_capped():
    assert 1 <= len(blocks_between((59.95, 24.3), (60.0, 24.5))) <= 20
    assert len(blocks_between((59.8, 20.0), (65.0, 25.0))) > 60


def test_router_downloads_then_plans_on_simulated_chart(tmp_path):
    async def run():
        chart = VectorChart(tmp_path, None, fake_chart_layer)
        router = AutoRouter(chart)
        start, end = (59.972, 24.60), (59.946, 24.40)  # along the simulated main fairway
        first = await router.route(start, end, 1.5)
        assert first["downloading"] > 0
        await router._downloading
        second = await router.route(start, end, 1.5)
        assert second["downloading"] == 0 and "error" not in second, second
        assert 2 <= len(second["points"]) <= 60
        assert second["metres"] < 1.3 * distance_m(*start, *end)
        # A 12 m draught boat can't use the 10 m fairway, only lines of unknown depth.
        deep = await router.route(start, end, 12)
        assert deep.get("error") or deep["points"] != second["points"]

    asyncio.run(run())


def test_start_joins_each_nearby_part_of_the_network():
    # A boat channel that stops short of the main fairway, and a start right by the channel:
    # the start must also join the main fairway, the end being on it.
    channel = merc(*[(59.99 - i * 0.002, 24.37 + i * 0.01) for i in range(7)])  # six short pieces
    main = merc((59.95, 24.30), (59.955, 24.50), (59.975, 24.62))
    g = build_graph([channel, main])
    assert len(set(g.comp)) == 2
    route = find_route(g, (59.986, 24.405), (59.976, 24.63))
    assert "error" not in route, route


def test_graph_kept_only_while_small(tmp_path, monkeypatch):
    from ferrywatch import autoroute

    async def run():
        chart = VectorChart(tmp_path, None, fake_chart_layer)
        router = AutoRouter(chart)
        start, end = (59.972, 24.60), (59.946, 24.40)
        await router.route(start, end, 1.5)
        await router._downloading
        await router.route(start, end, 1.5)
        assert router._cache is not None  # small: kept for re-planning
        monkeypatch.setattr(autoroute, "CACHE_MAX_BYTES", 0)
        await router.route(start, end, 2.0)
        assert router._cache is None  # too big: dropped right after use

    asyncio.run(run())
