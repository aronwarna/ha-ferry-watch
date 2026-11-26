import asyncio
import math
from pathlib import Path

from ferrywatch import satellite
from ferrywatch.satellite import Satellite, mercator_bbox, region


def tile(lat, lon, z):
    n = 2**z
    return (int((lon + 180) / 360 * n),
            int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n))


def test_region():
    assert region(14, *tile(60.1, 24.0, 14)) == "fi"       # Porkkala
    assert region(14, *tile(60.1, 19.95, 14)) == "fi"      # Mariehamn
    assert region(14, *tile(59.44, 24.75, 14)) == "ee"     # Tallinn
    assert region(14, *tile(58.25, 22.5, 14)) == "ee"      # Saaremaa
    assert region(14, *tile(59.33, 18.07, 14)) is None     # Stockholm
    assert region(14, *tile(59.93, 30.3, 14)) is None      # St Petersburg
    assert region(10, *tile(60.1, 24.0, 10)) is None       # too far out: Esri


def test_bbox():
    assert mercator_bbox(1, 0, 0) == "-20037508.343,0.000,0.000,20037508.343"


class FakeCache:
    def __init__(self, fail=()):
        self.failing = {}
        self.asked = []
        self.fail = fail

    async def get(self, source, z, x, y, url):
        self.asked.append((source, url))
        if source in self.fail:
            self.failing[source] = "down"
            return None
        return Path(f"/{source}")


def test_sources_and_fallback():
    z, (x, y) = 15, tile(60.1, 24.0, 15)
    cache = FakeCache()
    assert asyncio.run(Satellite(cache, "k").tile(z, x, y)) == Path("/fi")
    assert "kapsi" in cache.asked[0][1] and "EPSG:3857" in cache.asked[0][1]
    cache = FakeCache(fail={"fi"})
    assert asyncio.run(Satellite(cache, "k").tile(z, x, y)) == Path("/fi-mml")
    assert cache.asked[1][1].endswith(f"/{z}/{y}/{x}.jpg?api-key=k")
    assert asyncio.run(Satellite(FakeCache(fail={"fi"}), "").tile(z, x, y)) is None  # no key
    assert asyncio.run(Satellite(FakeCache(fail={"fi"}), "k").tile(17, *tile(60.1, 24.0, 17))) is None
    z, (x, y) = 15, tile(59.44, 24.75, 15)
    cache = FakeCache()
    asyncio.run(Satellite(cache).tile(z, x, y))
    assert cache.asked[0][1].endswith(f"/foto@GMC/{z}/{x}/{2**z - 1 - y}.png")
    assert asyncio.run(Satellite(FakeCache()).tile(15, *tile(59.33, 18.07, 15))) is None
