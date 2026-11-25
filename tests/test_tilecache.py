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
        os.utime(p, (1000 + 60 * i, 1000 + 60 * i))
    assert cache.prune() == 1
    assert not cache.path("chart", 1, 0, 0).exists() and cache.path("chart", 1, 0, 2).exists()


def test_old_copy_kept_when_service_fails_or_says_nothing(tmp_path):
    """Old pictures stay until a better one comes: a failure, a "nothing here" (404) or an
    unreadable answer never replaces a picture already kept."""
    session = FakeSession({"a": FakeResponse(404), "b": FakeResponse(500), "c": FakeResponse(200, b"junk")})
    cache = TileCache(tmp_path, session, delete_stale=False)
    cache.max_ages = {"old": 10}
    for y in range(3):
        p = cache.path("old", 5, 1, y)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"PICTURE")
        os.utime(p, (1000, 1000))  # long past its max age: asked for again

    def bad(body):
        raise ValueError("not a picture")

    async def run():
        return [await cache.get("old", 5, 1, 0, "a"), await cache.get("old", 5, 1, 1, "b"),
                await cache.get("old", 5, 1, 2, "c", transform=bad)]

    got = asyncio.run(run())
    assert all(p is not None and p.read_bytes() == b"PICTURE" for p in got)
    assert session.calls == ["a", "b", "c"]
    assert got[0].stat().st_mtime > 1000 and got[2].stat().st_mtime > 1000  # answered: checked now
    assert got[1].stat().st_mtime == 1000  # service down: asked again next time
    for p in got:
        os.utime(p, (1000, 1000))
    assert cache.prune() == 0 and all(p.exists() for p in got)  # never removed for age


def test_stale_tiles_removed_when_allowed(tmp_path):
    cache = TileCache(tmp_path, None)
    cache.max_ages = {"daily": 10}
    p = cache.path("daily", 5, 1, 1)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x")
    os.utime(p, (1000, 1000))
    assert cache.prune() == 1 and not p.exists()
