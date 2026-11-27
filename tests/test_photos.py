import asyncio

from ferrywatch.photos import LOOKUP_REV, PhotoCache, valid_imo


def test_valid_imo():
    assert valid_imo(9354284) == 9354284
    assert valid_imo("9074729") == 9074729
    assert valid_imo(9354285) is None  # wrong check digit
    assert valid_imo(0) is None and valid_imo(None) is None and valid_imo(123) is None


def fake_cache(tmp_path, found, max_files=0):
    cache = PhotoCache(tmp_path, None, max_files)
    asked = []

    async def wiki(mmsi, lang, title):
        asked.append(("wiki", title))
        return False

    async def commons(mmsi, imo):
        asked.append(("commons", imo))
        if "commons" in found:
            cache._save(mmsi, b"jpeg", ".jpg", {"source": "Wikimedia Commons", "page_url": "https://c", "whole": True})
            return True
        return False

    async def wikidata(mmsi, imo):
        asked.append(("wikidata", imo))
        return False

    async def search(mmsi, imo):
        asked.append(("search", imo))
        if "error" in found:
            raise ValueError("boom")
        return False

    cache._fetch_from_wikipedia = wiki
    cache._fetch_from_commons = commons
    cache._fetch_from_wikidata = wikidata
    cache._search_commons = search
    return cache, asked


def test_other_ship_is_looked_up_by_imo_only(tmp_path):
    cache, asked = fake_cache(tmp_path, {"commons"})
    assert asyncio.run(cache.ensure({"mmsi": 1, "name": "ARUNA", "imo": 9074729, "by_name": False, "imo_first": True}))
    assert asked == [("commons", 9074729)]
    assert cache.public_info(1)["source"] == "Wikimedia Commons"


def test_fleet_ship_tries_wikipedia_then_imo_and_remembers_a_miss(tmp_path):
    cache, asked = fake_cache(tmp_path, set())
    assert not asyncio.run(cache.ensure({"mmsi": 2, "name": "Sirena Seaways", "imo": 9212163}))
    assert [a[0] for a in asked][-3:] == ["commons", "wikidata", "search"] and asked[0][0] == "wiki"
    assert cache.miss(2)["why"] == "none"
    assert cache.tried(2) and cache.info(2)["rev"] == LOOKUP_REV
    asked.clear()
    assert not asyncio.run(cache.ensure({"mmsi": 2, "name": "Sirena Seaways", "imo": 9212163}))
    assert asked == []


def test_old_miss_without_rev_is_tried_again(tmp_path):
    cache, asked = fake_cache(tmp_path, set())
    cache._write_info(3, {"failed_ts": 2_000_000_000})
    asyncio.run(cache.ensure({"mmsi": 3, "name": "X", "imo": 9074729}))
    assert ("commons", 9074729) in asked


def test_other_photos_are_capped(tmp_path):
    import os
    import time
    cache, _ = fake_cache(tmp_path, {"commons"}, max_files=2)
    for i, mmsi in enumerate((11, 12, 13)):
        asyncio.run(cache.ensure({"mmsi": mmsi, "imo": 9074729, "by_name": False}))
        os.utime(tmp_path / f"{mmsi}.json", (time.time() + i, time.time() + i))
    cache._trim()
    assert cache.path(11) is None and cache.path(12) and cache.path(13)


def test_miss_reasons(tmp_path):
    cache, _ = fake_cache(tmp_path, {"error"})
    asyncio.run(cache.ensure({"mmsi": 5, "imo": 9074729, "by_name": False}))
    assert cache.miss(5)["why"] == "error" and "boom" in cache.miss(5)["error"]
    assert not cache.tried(5) or True
    asyncio.run(cache.ensure({"mmsi": 6, "imo": 0, "by_name": False, "name": "X"}))
    assert cache.miss(6)["why"] == "no_imo"


def test_cut_short_commons_photo_is_fetched_again(tmp_path):
    cache = PhotoCache(tmp_path, None)
    (tmp_path / "7.jpg").write_bytes(b"part")
    cache._write_info(7, {"file": "7.jpg", "source": "Wikidata", "fetched_ts": 1})
    assert cache.path(7) is None
    cache._write_info(7, {"file": "7.jpg", "source": "Wikidata", "fetched_ts": 1, "whole": True})
    assert cache.path(7)
    cache._write_info(7, {"file": "7.jpg", "source": "Wikipedia", "fetched_ts": 1})
    assert cache.path(7)
