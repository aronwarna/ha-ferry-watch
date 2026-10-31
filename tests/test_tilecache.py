import asyncio
import os

from ferrywatch.tilecache import TileCache


class FakeResponse:
    def __init__(self, status, body=b"", content_type="image/png"):
        self.status, self.body, self.content_type = status, body, content_type

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def raise_for_status(self):
        if self.status >= 400:
            import aiohttp
            raise aiohttp.ClientError(str(self.status))

    async def read(self):
        return self.body


class FakeSession:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def get(self, url, **kw):
        self.calls.append(url)
        return self.responses[url]


def test_fetches_once_then_serves_from_disk(tmp_path):
    session = FakeSession({"u/1": FakeResponse(200, b"PNG"), "u/2": FakeResponse(404),
                           "u/3": FakeResponse(200, b"<xml/>", "text/xml")})
    cache = TileCache(tmp_path, session)

    async def run():
        first = await cache.get("chart", 5, 1, 1, "u/1")
        again = await cache.get("chart", 5, 1, 1, "u/1")
        empty = await cache.get("seamark", 5, 1, 2, "u/2")
        empty_again = await cache.get("seamark", 5, 1, 2, "u/2")
        error = await cache.get("chart", 5, 1, 3, "u/3")
        return first, again, empty, empty_again, error

    first, again, empty, empty_again, error = asyncio.run(run())
    assert first == again and first.read_bytes() == b"PNG"
    assert empty is None and empty_again is None  # "no tile here" is remembered too
    assert error is None and not cache.path("chart", 5, 1, 3).exists()  # error pages aren't kept
    assert session.calls == ["u/1", "u/2", "u/3"]


def test_prune_removes_oldest(tmp_path):
    cache = TileCache(tmp_path, None, max_bytes=10)
    for i in range(3):
        p = cache.path("chart", 1, 0, i)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x" * 5)
        os.utime(p, (1000 + i, 1000 + i))
    assert cache.prune() == 1
    assert not cache.path("chart", 1, 0, 0).exists() and cache.path("chart", 1, 0, 2).exists()
