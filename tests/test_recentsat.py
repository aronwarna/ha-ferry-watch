import asyncio
from pathlib import Path

from ferrywatch import recentsat
from ferrywatch.recentsat import RecentSatellite, color_formula, contains, parse_box, snap_box, summarize

SQUARE = {"type": "Polygon", "coordinates": [[[24, 59], [25, 59], [25, 61], [24, 61], [24, 59]]]}
WEST_HALF = {"type": "Polygon", "coordinates": [[[23, 59], [24.5, 59], [24.5, 61], [23, 61], [23, 59]]]}


def _item(id_, when, cloud, geom):
    return {"id": id_, "geometry": geom, "properties": {"datetime": when, "eo:cloud_cover": cloud}}


def test_contains_polygon_hole_and_multi():
    assert contains(SQUARE, 24.5, 60)
    assert not contains(SQUARE, 25.5, 60)
    holed = {"type": "Polygon", "coordinates": [SQUARE["coordinates"][0], [[24.4, 59.9], [24.6, 59.9], [24.6, 60.1], [24.4, 60.1], [24.4, 59.9]]]}
    assert not contains(holed, 24.5, 60)
    multi = {"type": "MultiPolygon", "coordinates": [SQUARE["coordinates"], WEST_HALF["coordinates"]]}
    assert contains(multi, 23.5, 60)
    assert not contains(None, 0, 0)


def test_summarize_days_cover_and_cloud():
    box = (59.9, 24.0, 60.1, 25.0)
    items = [
        _item("S2B_a", "2026-11-25T10:02:49Z", 80, SQUARE),
        _item("S2B_b", "2026-11-25T10:02:49Z", 10, WEST_HALF),  # least cloudy one wins where both are
        _item("S2C_c", "2026-11-22T09:52:29Z", 5, WEST_HALF),
        _item("S2A_d", "2026-11-20T10:00:00Z", 50, {"type": "Polygon", "coordinates": [[[30, 59], [31, 59], [31, 60], [30, 59]]]}),
    ]
    days = summarize(items, box)
    assert [d["date"] for d in days] == ["2026-11-25", "2026-11-22"]  # the 20th misses the view
    d25, d22 = days
    assert d25["cover"] == 1.0
    assert 10 < d25["cloud"] < 80  # west half 10 %, east half 80 %
    assert d25["sats"] == ["S2B"] and d25["time"] == "10:02"
    assert 0.4 < d22["cover"] < 0.6 and d22["cloud"] == 5
    assert not d22["clear"]  # too little of the view
    assert not d25["clear"] or d25["cloud"] <= recentsat.CLEAR_CLOUD


def test_snap_box_and_parse():
    assert snap_box((59.93, 24.12, 60.02, 24.61)) == (59.75, 24.0, 60.25, 24.75)
    assert parse_box("59.9,24.1,60.1,24.6") == (59.9, 24.1, 60.1, 24.6)
    for bad in ("1,2,3", "60,24,59,25", "0,0,30,1", "x,1,2,3"):
        try:
            parse_box(bad)
        except ValueError:
            continue
        raise AssertionError(bad)


def test_color_formula_brighter_in_winter():
    assert "2.1" in color_formula("2026-11-25")
    assert "1.7" in color_formula("2026-07-10")


class _Resp:
    def __init__(self, status, body=b"", ctype="image/webp", data=None):
        self.status, self.body, self.content_type, self.data = status, body, ctype, data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def raise_for_status(self):
        if self.status >= 400:
            import aiohttp
            raise aiohttp.ClientError(str(self.status))

    async def read(self):
        return self.body

    async def json(self, content_type=None):
        return self.data


class _Session:
    """Registers give a new id each time; the first id has expired (410)."""

    def __init__(self):
        self.registered = 0
        self.tiles = []

    def post(self, url, json=None, **kw):
        self.registered += 1
        return _Resp(200, data={"id": f"sid{self.registered}"})

    def get(self, url, **kw):
        self.tiles.append(url)
        if "/sid1/" in url:
            return _Resp(410, ctype="application/json")
        if "/9/" in url:
            return _Resp(204, ctype="")
        return _Resp(200, b"RIFFxxxxWEBP")


def test_tile_reregisters_on_410_and_keeps_nothing_here(tmp_path: Path):
    async def run():
        s = _Session()
        rs = RecentSatellite(tmp_path, s)
        p = await rs.tile("2026-11-05", 12, 2325, 1189)
        assert p and p.read_bytes().startswith(b"RIFF")
        assert s.registered == 2 and "/sid2/" in s.tiles[-1]
        assert "color_formula=Gamma+RGB+2.1" in s.tiles[-1]
        # kept: no second download
        n = len(s.tiles)
        assert await rs.tile("2026-11-05", 12, 2325, 1189) == p and len(s.tiles) == n
        # 204: nothing that day here, remembered
        assert await rs.tile("2026-11-05", 9, 290, 148) is None
        assert await rs.tile("2026-11-05", 9, 290, 148) is None and len(s.tiles) == n + 1
    asyncio.run(run())


def test_fake_days_and_tiles(tmp_path: Path):
    async def run():
        rs = RecentSatellite(tmp_path, None, fake=True)
        data = await rs.days((59.9, 24.0, 60.1, 24.8))
        assert data["days"] and data["best"]
        best = next(d for d in data["days"] if d["date"] == data["best"])
        assert best["clear"]
        p = await rs.tile(data["best"], 12, 2325, 1189)
        assert p and p.stat().st_size > 100
    asyncio.run(run())
