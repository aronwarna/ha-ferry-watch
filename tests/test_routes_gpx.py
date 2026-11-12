"""Saved routes as GPX for a chart plotter, GPX files as saved routes, and the terrain map's tiles."""
import xml.etree.ElementTree as ET

from ferrywatch.__main__ import TOPO_MAX_Z, topo_covers
from ferrywatch.web import MAX_ROUTE_POINTS, route_to_gpx, routes_from_gpx


def test_route_out_and_back_in():
    route = {"id": 3, "name": "Home <-> Porkkala & back", "points": [[60.1, 24.9], [60.05, 24.7], [59.98, 24.4]],
             "plan": {"wp": [0, 2], "fw": [1]}}
    text = route_to_gpx(route)
    root = ET.fromstring(text)  # well-formed, with the name escaped
    names = [el.text for el in root.iter("{http://www.topografix.com/GPX/1/1}name")]
    assert names[:2] == [route["name"]] * 2 and names[2:] == ["01", "t001", "02"]
    assert routes_from_gpx(text.encode()) == [(route["name"], route["points"])]


def test_tracks_are_read_when_there_is_no_route_and_thinned():
    pts = "".join(f'<trkpt lat="{60 + i / 10000}" lon="24.5"/>' for i in range(MAX_ROUTE_POINTS * 3))
    gpx = f'<gpx><trk><name>Trip</name><trkseg>{pts}<trkpt lat="x" lon="1"/></trkseg></trk></gpx>'.encode()
    (name, points), = routes_from_gpx(gpx)
    assert name == "Trip" and len(points) == MAX_ROUTE_POINTS
    assert points[0] == [60.0, 24.5] and points[-1][0] == round(60 + (MAX_ROUTE_POINTS * 3 - 1) / 10000, 6)
    assert routes_from_gpx(b"<gpx><rte><rtept lat='60' lon='24'/></rte></gpx>") == []


def test_terrain_map_only_over_finland():
    assert topo_covers(10, 581, 296)       # Helsinki
    assert not topo_covers(10, 550, 300)   # Stockholm
    assert not topo_covers(TOPO_MAX_Z + 1, 581 * 128, 296 * 128)
    assert not topo_covers(4, 9, 4)
