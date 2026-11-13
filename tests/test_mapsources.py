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
    assert years[0] == 1969 and years[-1] == 2026 and len(years) == 16


def test_old_map_rule_reads_titles():
    found = [{"name": "paituli:x1", "title": "Pitäjänkartta 1935"}, {"name": "paituli:corine", "title": "Corine"},
             {"name": "paituli:y", "title": "Peruskartta 1:20 000 (1960)"}]
    rows = mapsources.layers_from_names(MML, found)
    assert {(r["label"], r["year"]) for r in rows} == {("Parish map", 1935), ("Basic map", 1960)}


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
    assert "LAYERS=Ortokartta_1969" in url and "SRS=EPSG:3857" in url and "FORMAT=image/jpeg" in url
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
    assert buildings.covers(9325, 4740) and not buildings.covers(9200, 4800)


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
    assert all("{date}" in t for t in gibs["templates"])


def test_wider_photo_sources():
    from ferrywatch import simmaps
    got = {}
    for sid in ("hsy-region", "turku", "tampere", "oulu", "estonia-old", "mml-old"):
        src = next(s for s in mapsources.SOURCES if s["id"] == sid)
        rows = mapsources.layers_from_names(src, simmaps.fake_capabilities(src))
        got[sid] = sorted((r["group"], r["year"]) for r in rows)
    assert got["hsy-region"] == [("photo", 2015), ("photo", 2019), ("photo", 2023)]  # no false-colour
    assert ("photo", 1939) in got["turku"] and len(got["turku"]) == 3
    assert got["tampere"] == [("photo", 1946), ("photo", 1966)]
    assert got["estonia-old"] == [("map", 1900), ("map", 1930), ("photo", 1993), ("photo", 2005)]
    assert ("photo", 2009) in got["mml-old"] and sum(1 for g in got["mml-old"] if g[0] == "photo") == 1
    turku = next(s for s in mapsources.SOURCES if s["id"] == "turku")
    assert len(mapsources.known_layers(turku)) == 7


def test_wayback_and_yearly_sources():
    cfg = {"10": {"itemTitle": "World Imagery (Wayback 2014-02-20)"}, "999": {"itemTitle": "World Imagery (Wayback 2014-11-05)"},
           "31144": {"itemTitle": "World Imagery (Wayback 2019-06-26)"}, "x": {"itemTitle": "junk"}}
    assert mapsources.wayback_releases(cfg) == {2014: 999, 2019: 31144}
    s2 = next(s for s in mapsources.SOURCES if s["id"] == "s2")
    assert "s2cloudless-2020_3857" in s2["years"][2020] and "{z}/{y}/{x}" in s2["years"][2020]
