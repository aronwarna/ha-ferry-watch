import asyncio

from ferrywatch import buildings, overpass, parcels


def test_pretty_id_and_compact():
    assert parcels.pretty_id("25747000030028") == "257-470-3-28"
    assert parcels.pretty_id("257-470-3-28") == "257-470-3-28"
    assert parcels.pretty_id("hello") is None
    rows = parcels.compact({"features": [
        {"geometry": {"type": "Point", "coordinates": [24.4, 60.0]}, "properties": {"kiinteistotunnus": "25747000030028"}},
        {"geometry": {"type": "Point", "coordinates": [24.4, 60.0]}, "properties": {"kiinteistotunnus": "25747000030028"}},
        {"geometry": {"type": "Point", "coordinates": [24.5, 60.1]}, "properties": {"somethingNew": "257-470-3-75"}},
        {"geometry": {"type": "Polygon", "coordinates": []}, "properties": {}},
    ]})
    assert rows == [[60.0, 24.4, "257-470-3-28"], [60.1, 24.5, "257-470-3-75"]]


def test_ryhti_rows_and_cover():
    rows = buildings.compact_ryhti({"features": [
        {"geometry": {"type": "Point", "coordinates": [23.0, 60.0]},
         "properties": {"completion_date": "1987-06-01", "main_purpose": "0110", "number_of_storeys": 2}},
        {"geometry": {"type": "Point", "coordinates": [23.1, 60.0]}, "properties": {}},
    ]})
    assert rows[0][:5] == [60.0, 23.0, 1987, "0110", 2] and rows[1][2] is None
    # Hanko (far outside the capital region) is covered, and asked from the national register.
    import math
    def tile(lat, lon, z=14):
        n = 2 ** z
        return int((lon + 180) / 360 * n), int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n)
    assert buildings.covers(*tile(59.83, 22.97)) and not buildings.in_hsy(*tile(59.83, 22.97))
    assert buildings.in_hsy(*tile(60.17, 24.94))


def test_plain_errors():
    assert overpass.plain_error("HTTP 502: # 502 Gateway Timeout This Overpass API server is currently at capacity") == "its servers are busy"
    assert overpass.plain_error(asyncio.TimeoutError()) == "it took too long to answer"
    assert overpass.plain_error("502, message='Bad Gateway', url='https://overpass-api.de/api/interpreter'") == "its servers are busy"
    assert overpass.plain_error(None) == ""


def test_overpass_falls_back_to_a_mirror():
    class Resp:
        def __init__(self, status, body):
            self.status, self._body = status, body
            self.content = self
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def read(self, n=-1): return self._body
        async def iter_chunked(self, n):
            yield self._body
    class Session:
        def __init__(self): self.asked = []
        def post(self, url, **kw):
            self.asked.append(url)
            return Resp(502, b"busy") if "first" in url else Resp(200, b'{"elements": [1]}')
    s = Session()
    overpass._last_good = 0
    data = asyncio.run(overpass.fetch(s, "q", mirrors=("https://first/x", "https://second/x"), pause=0))
    assert data == {"elements": [1]} and s.asked == ["https://first/x", "https://second/x"]
    asyncio.run(overpass.fetch(s, "q", mirrors=("https://first/x", "https://second/x"), pause=0))
    assert s.asked[-1] == "https://second/x" and len(s.asked) == 3  # the one that worked is asked first
    overpass._last_good = 0
