from pathlib import Path

from ferrywatch import buildings, mapsources

ESPOO = next(s for s in mapsources.SOURCES if s["id"] == "espoo")
HELSINKI = next(s for s in mapsources.SOURCES if s["id"] == "helsinki")
MML = next(s for s in mapsources.SOURCES if s["id"] == "mml-old")

CAPS_111 = """<?xml version="1.0"?>
<WMT_MS_Capabilities version="1.1.1"><Capability><Layer><Title>Root</Title>
<LatLonBoundingBox minx="24.4" miny="60.0" maxx="24.9" maxy="60.4"/>
<Layer><Name>Ortokartta_1969</Name><Title>Espoon ortokartta 1969</Title></Layer>
<Layer><Name>Ortokartta</Name><Title>Espoon ortokartta 2026</Title>
<LatLonBoundingBox minx="24.5" miny="60.1" maxx="24.8" maxy="60.3"/></Layer>
<Layer><Name>Kantakartta</Name><Title>Kantakartta</Title></Layer>
</Layer></Capability></WMT_MS_Capabilities>"""

CAPS_130 = """<?xml version="1.0"?>
<WMS_Capabilities xmlns="http://www.opengis.net/wms" version="1.3.0"><Capability><Layer>
<Layer><Name>avoindata:Ortoilmakuva_2019_5cm</Name><Title>a</Title>
<EX_GeographicBoundingBox><westBoundLongitude>24.8</westBoundLongitude><eastBoundLongitude>25.2</eastBoundLongitude>
<southBoundLatitude>60.1</southBoundLatitude><northBoundLatitude>60.3</northBoundLatitude></EX_GeographicBoundingBox></Layer>
<Layer><Name>avoindata:Ortoilmakuva_2019_20cm</Name><Title>b</Title></Layer>
<Layer><Name>avoindata:Ortoilmakuva_1932</Name><Title>c</Title></Layer>
<Layer><Name>avoindata:Vaaravariortoilmakuva_2019</Name><Title>d</Title></Layer>
<Layer><Name>avoindata:Senaatinkartta_1870</Name><Title>Senaatinkartta</Title></Layer>
</Layer></Capability></WMS_Capabilities>"""


def test_parse_capabilities_both_versions(tmp_path: Path):
    p = tmp_path / "c.xml"
    p.write_text(CAPS_111)
    rows = {r["name"]: r for r in mapsources.parse_capabilities(p)}
    assert set(rows) == {"Ortokartta_1969", "Ortokartta", "Kantakartta"}
    assert rows["Ortokartta_1969"]["box"] == (60.0, 24.4, 60.4, 24.9)  # inherited from the parent
    assert rows["Ortokartta"]["box"] == (60.1, 24.5, 60.3, 24.8)
    p.write_text(CAPS_130)
    rows = {r["name"]: r for r in mapsources.parse_capabilities(p)}
    assert rows["avoindata:Ortoilmakuva_2019_5cm"]["box"] == (60.1, 24.8, 60.3, 25.2)


def test_rules_pick_years_and_one_photo_per_year(tmp_path: Path):
    p = tmp_path / "c.xml"
    p.write_text(CAPS_111)
    rows = mapsources.layers_from_names(ESPOO, mapsources.parse_capabilities(p))
    assert [(r["group"], r["year"]) for r in rows] == [("photo", 1969), ("photo", 2026)]
    p.write_text(CAPS_130)
    rows = mapsources.layers_from_names(HELSINKI, mapsources.parse_capabilities(p))
    photos = {r["year"]: r["layer"] for r in rows if r["group"] == "photo"}
    assert photos == {1932: "avoindata:Ortoilmakuva_1932", 2019: "avoindata:Ortoilmakuva_2019_20cm"}
    maps = [r for r in rows if r["group"] == "map"]
    assert len(maps) == 1 and maps[0]["year"] == 1870 and maps[0]["label"] == "Senate map"


def test_known_layers_work_without_capabilities():
    rows = mapsources.known_layers(ESPOO)
    years = sorted(r["year"] for r in rows if r["group"] == "photo")
    assert years[0] == 1950 and years[-1] == 2026 and len(years) == 17


def test_old_map_series_match_exact_names():
    found = [{"name": "mml_pitajakartta_20k_1912-1968", "title": "x"}, {"name": "corine", "title": "Corine"},
             {"name": "mml_topo_20k_1960-1969", "title": "y"}, {"name": "mml_yleiskartta_400k_1863-1879", "title": "z"}]
    rows = {r["label"]: r for r in mapsources.layers_from_names(MML, found)}
    assert set(rows) == {"Parish map 1:20 000", "Basic map 1:20 000", "General map 1:400 000"}
    assert (rows["Parish map 1:20 000"]["year"], rows["Parish map 1:20 000"]["year_to"]) == (1912, 1968)
    assert rows["General map 1:400 000"]["max_zoom"] < rows["Parish map 1:20 000"]["max_zoom"]
    # every known series name has its own rule, so the fallback lists all of them
    assert len(mapsources.known_layers(MML)) == len(MML["known"])


def test_years_and_base_names():
    assert mapsources._years("x", "Pitäjänkartta 1912-1968") == (1912, 1968)
    assert mapsources._years("avoindata:Ortoilmakuva_2019_20cm", "") == (2019, None)  # 20 cm is not a year
    assert mapsources._years("a", "Senaatinkartta 192x") == (1920, 1929)
    assert mapsources._base("avoindata:Ortoilmakuva_2019_20cm") == mapsources._base("avoindata:Ortoilmakuva_1932")


def test_tile_urls_and_coverage(tmp_path):
    class Store:
        def kv_get(self, k, d=None):
            return {"mml_api_key": "KEY"}.get(k, d)

        def kv_set(self, k, v):
            pass

    ms = mapsources.MapSources(tmp_path, session=None, store=Store())
    lid = "espoo-ortokartta-1969"
    assert ms.covers(lid, 14, 9318, 4739) is True       # Espoo
    assert ms.covers(lid, 14, 9300, 4790) is False      # far south at sea
    assert ms.covers("nope", 14, 1, 1) is None
    url = ms.tile_urls(ms._layers[lid], 14, 9318, 4739)[0]
    assert "LAYERS=Ortokartta_1969" in url and "SRS=EPSG:3857" in url and "FORMAT=image/png" in url
    assert "TRANSPARENT=TRUE" in url
    hel = ms.tile_urls(ms._layers["helsinki-ortoilmakuva-1932"], 14, 9318, 4739)[0]
    assert "FORMAT=image/vnd.jpeg-png" in hel  # see-through outside the photo, not white
    est = next(r for r in ms._layers.values() if r["source"] == "estonia-old" and r["group"] == "photo")
    assert "SRS=EPSG:3301" in ms.tile_urls(est, 12, 2312, 1170)[0]
    prop = ms.tile_urls(ms._layers["property"], 15, 18640, 9480)
    assert "api-key=KEY" in prop[0] and "kapsi" in prop[1]
    names = {r["id"] for r in ms.listing()["layers"]}
    assert {"property", "property-ids", "seabed"} <= names


def test_buildings_compact():
    rows = buildings.compact({"features": [
        {"geometry": {"type": "Point", "coordinates": [24.9, 60.2]},
         "properties": {"kavu": 1972.0, "katu": "Rantatie", "osno1": 5.0, "kayttarks": "Rivitalot", "kerrosten_lkm": 2.0}},
        {"geometry": {"type": "Polygon", "coordinates": [[[24.0, 60.0], [24.2, 60.0], [24.2, 60.2], [24.0, 60.2]]]},
         "properties": {"kavu": 0}},
        {"geometry": None, "properties": {}},
    ]})
    assert rows[0][:6] == [60.2, 24.9, 1972, "Rivitalot", 2, "Rantatie 5"]
    assert rows[1][2] is None and abs(rows[1][0] - 60.1) < 0.06
    assert len(rows) == 2
    assert buildings.covers(9325, 4740) and buildings.in_hsy(9325, 4740) and not buildings.in_hsy(9200, 4800)
    assert not buildings.covers(8000, 6000)


def test_daily_and_latest_only():
    assert mapsources.daily_date(86400 * 10 + 12 * 3600) == "1970-01-10"
    assert mapsources.daily_date(86400 * 10 + 14 * 3600) == "1970-01-11"
    src = next(s for s in mapsources.SOURCES if s["id"] == "emodnet-density")
    rows = mapsources.layers_from_names(src, [
        {"name": "emodnet:2022_st_All_avg", "title": "Vessel density 2022"},
        {"name": "emodnet:2023_st_All_avg", "title": "Vessel density 2023"},
        {"name": "emodnet:2023_st_01_avg", "title": "Vessel density 2023 fishing"},
    ])
    assert [(r["year"], r["label"], r["group"]) for r in rows] == [(2023, "Ship traffic density", "overlay")]
    gibs = next(s for s in mapsources.SOURCES if s["id"] == "gibs-today")
    assert all("{date" in t for t in gibs["templates"]) and len(gibs["templates"]) == 10


def test_wider_photo_sources():
    from ferrywatch import simmaps
    got = {}
    for sid in ("hsy-region", "turku", "oulu", "estonia-old", "vantaa"):
        src = next(s for s in mapsources.SOURCES if s["id"] == sid)
        rows = mapsources.layers_from_names(src, simmaps.fake_capabilities(src))
        got[sid] = sorted((r["group"], r["year"]) for r in rows)
    assert got["hsy-region"] == [("photo", 2015), ("photo", 2019), ("photo", 2023)]  # no false-colour
    assert ("photo", 1939) in got["turku"] and ("map", 1950) in got["turku"]
    assert ("photo", 1993) in got["estonia-old"] and ("map", 1894) in got["estonia-old"]
    assert ("map", 1776) in got["vantaa"] and ("photo", 1954) in got["vantaa"]
    assert not any(s["id"] == "tampere" for s in mapsources.SOURCES)


def test_wayback_and_yearly_sources():
    cfg = {"10": {"itemTitle": "World Imagery (Wayback 2014-02-20)"}, "999": {"itemTitle": "World Imagery (Wayback 2014-11-05)"},
           "31144": {"itemTitle": "World Imagery (Wayback 2019-06-26)"}, "x": {"itemTitle": "junk"}}
    assert mapsources.wayback_releases(cfg) == {2014: 999, 2019: 31144}
    s2 = next(s for s in mapsources.SOURCES if s["id"] == "s2")
    assert "s2cloudless-2020_3857" in s2["years"][2020] and "{z}/{y}/{x}" in s2["years"][2020]


def test_kirkkonummi_folder_and_sipoo_tiles(tmp_path):
    import asyncio
    import io

    from PIL import Image

    class Store:
        def kv_get(self, k, d=None):
            return d

        def kv_set(self, k, v):
            pass

    kirkko = next(s for s in mapsources.SOURCES if s["id"] == "kirkkonummi")
    found = [{"name": f"Historia_aineisto/{n}"} for n in ("Ilmakuva_1944", "Ilmakuva_1954_1956", "ortokuva_2017",
                                                         "Parenteesialueen_raja", "Pitäjänkartta_1932_1943")]
    rows = {r["id"]: r for r in mapsources.layers_from_names(kirkko, found)}
    assert set(rows) == {"kirkkonummi-ilmakuva-1944", "kirkkonummi-ilmakuva-1954-1956", "kirkkonummi-ortokuva-2017",
                         "kirkkonummi-pit-j-nkartta-1932-1943"}  # the lease border is an overlay, not a year
    assert (rows["kirkkonummi-ilmakuva-1954-1956"]["year"], rows["kirkkonummi-ilmakuva-1954-1956"]["year_to"]) == (1954, 1956)

    ms = mapsources.MapSources(tmp_path, session=None, store=Store())
    url = ms.tile_urls(ms._layers["kirkkonummi-ilmakuva-1944"], 14, 9300, 4740)[0]
    assert "/Historia_aineisto/Ilmakuva_1944/MapServer/export?" in url and "imageSR=3857" in url
    assert "porkkala-lease" in ms._layers and ms._layers["porkkala-lease"]["group"] == "overlay"

    sipoo = ms._layers["sipoo-1987"]
    assert mapsources._needs_cover(sipoo) and not sipoo["wide"]
    asked = []

    async def fake_get(url, timeout):
        asked.append(url)
        z, y, x = (int(v) for v in url.rsplit("/", 3)[1:])
        if x % 2:
            raise ValueError("no tile")
        buf = io.BytesIO()
        Image.new("RGB", (256, 256), (90, 110, 80)).save(buf, "PNG")
        return buf.getvalue()

    ms._get = fake_get
    img = asyncio.run(ms._stitch(sipoo, mapsources.box_merc(sipoo["box"]), (300, 400)))
    assert img.size == (300, 400) and 1 < len(asked) <= 24  # its first zoom (11) already needs 20
    alphas = img.getchannel("A").getextrema()
    assert alphas == (0, 255)  # missing tiles stay see-through


class _KV:
    def __init__(self, data=None):
        self.data = dict(data or {})

    def kv_get(self, k, d=None):
        return self.data.get(k, d)

    def kv_set(self, k, v):
        self.data[k] = v


def test_kept_store_and_save_plan(tmp_path):
    ms = mapsources.MapSources(tmp_path, session=None, store=_KV())
    row = ms._layers["espoo-ortokartta-1969"]
    assert mapsources._historic(row) and ms.kept.delete_stale is False
    assert ms.kept.max_age("espoo") == mapsources.KEPT_RECHECK
    box = (60.15, 24.70, 60.17, 24.74)  # a small piece of Espoo
    plan = ms.save_plan(box, [row["id"], "property"], 15)  # property isn't an old picture: left out
    assert plan["layers"] == 1 and plan["tiles"] > 0 and plan["have"] == 0 and not plan["too_big"]
    tiles = list(ms._save_tiles(box, [row["id"]], 15))
    assert {z for _, z, _, _ in tiles} == set(range(max(row["min_zoom"], mapsources.SAVE_MIN_ZOOM), 16))
    _, z, x, y = tiles[-1]
    p = ms.kept.path(mapsources.kept_folder(row), z, x, y)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"PNG")
    assert ms.save_plan(box, [row["id"]], 15)["have"] == 1
    assert ms.save_plan((59.0, 19.0, 61.0, 26.0), [row["id"]], 19)["too_big"] or row["max_zoom"] < 17


def test_layers_no_longer_listed_stay_offered(tmp_path):
    import asyncio

    ms = mapsources.MapSources(tmp_path, session=None, store=_KV())
    lists = [[{"name": "Ortokartta_1969"}, {"name": "Ortokartta_1988"}], [{"name": "Ortokartta_1988"}]]

    async def caps(url):
        return lists.pop(0)

    ms._capabilities = caps
    asyncio.run(ms.discover(ESPOO))
    asyncio.run(ms.discover(ESPOO))
    assert {e["name"] for e in ms.found["espoo"]} == {"Ortokartta_1969", "Ortokartta_1988"}


def test_home_save_skipped_in_simulator(tmp_path):
    import asyncio

    fake = mapsources.MapSources(tmp_path / "a", session=None, store=_KV(), fake=True)
    asyncio.run(fake.start_home_save())
    assert fake._save_task is None


def test_home_save_picks_sharpest_zoom_that_fits(tmp_path, monkeypatch):
    import asyncio

    from ferrywatch.histclean import Cover

    kv = _KV({"maps_kept_gb": 10})
    ms = mapsources.MapSources(tmp_path, session=None, store=kv)
    rows = [r for r in ms._layers.values() if r["source"] == "kirkkonummi" and mapsources._historic(r)]
    if not rows:  # no folder listing kept: make two layers like the service's
        kirkko = next(s for s in mapsources.SOURCES if s["id"] == "kirkkonummi")
        ms.found["kirkkonummi"] = [{"name": "Historia_aineisto/Ilmakuva_1944"},
                                   {"name": "Historia_aineisto/Pitäjänkartta_1932_1943"}]
        ms._rebuild()
        rows = [r for r in ms._layers.values() if r["source"] == "kirkkonummi" and mapsources._historic(r)]
    assert rows

    class Small:  # picture only in a small piece of Porkkala
        def any_in(self, m):
            x0, y0, x1, y1 = m
            return x1 > 2_710_000 and x0 < 2_716_000 and y1 > 8_405_000 and y0 < 8_411_000

    for r in rows:
        ms.cover[r["id"]] = Small()
    box, ids, sizes = ms._home_sizes()
    assert sizes[18][0] > sizes[17][0] > sizes[14][0] > 0
    maps = [r for r in rows if r["group"] == "map"]
    if maps:
        assert ms._best_zoom(maps[0], 18) == mapsources.BEST_MAP_ZOOM
    started = []

    async def fake_run(todo, auto=False):
        started.append(next(iter(todo), None))

    monkeypatch.setattr(ms, "_save_run", fake_run)
    monkeypatch.setattr(ms, "kept_bytes", lambda: 0)
    room = mapsources.HOME_FILL * 10 * 1024**3
    want = max(z for z, (_, b) in sizes.items() if b <= room)

    async def go():
        await ms.start_home_save()
        await ms._save_task
    asyncio.run(go())
    assert ms.save_state["zoom"] == want and started and started[0][1] == min(max(r["min_zoom"], mapsources.SAVE_MIN_ZOOM) for r in rows)  # coarse first
    kv.data["maps_home_zmax"] = want
    ms._save_task = None
    asyncio.run(ms.start_home_save())
    assert ms._save_task is None  # done already at that zoom


def test_kept_space_setting(tmp_path, monkeypatch):
    from collections import namedtuple

    import pytest

    import os

    vfs = namedtuple("vfs", "f_bavail f_frsize f_favail")
    monkeypatch.setattr(os, "statvfs", lambda p: vfs(30 * 1024**2, 1024, 5_000_000))
    kv = _KV()
    ms = mapsources.MapSources(tmp_path, session=None, store=kv)
    assert ms.kept_space()["kept_choices"] == [3, 5, 10, 20]  # 30 GB free, 4 GB kept back
    assert ms.kept.max_bytes == 10 * 1024**3  # not picked: the largest within half the room
    ms.set_kept_limit(20)
    assert kv.data["maps_kept_gb"] == 20 and ms.kept.max_bytes == 20 * 1024**3
    with pytest.raises(ValueError):
        ms.set_kept_limit(100)
    with pytest.raises(ValueError):
        ms.set_kept_limit(7)
    again = mapsources.MapSources(tmp_path, session=None, store=kv)
    assert again.kept.max_bytes == 20 * 1024**3
