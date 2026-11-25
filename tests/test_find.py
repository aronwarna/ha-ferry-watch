import asyncio
import gzip
import json
from types import SimpleNamespace

from ferrywatch import vchart
from ferrywatch.find import AidNames, Finder, ReserveNames, ais_type, fold, score


def test_fold_and_score():
    assert fold("Rönnbåda") == "ronnbada"
    assert fold("  Silja   SERENADE ") == "silja serenade"
    w = lambda q: fold(q).split()  # noqa: E731
    assert score("Rönnbåda", w("ronnbada"), "ronnbada") == 0
    assert score("Silja Serenade", w("silja"), "silja") == 1
    assert score("Viking Silja", w("sil"), "sil") == 2
    assert score("Parainen Utö", w("rain"), "rain") == 3
    assert score("Silja Serenade", w("silja star"), "silja star") is None
    assert score(None, w("a"), "a") is None


def test_ais_type():
    assert ais_type(60) == "Passenger" and ais_type(79) == "Cargo" and ais_type(52) == "Tug"
    assert ais_type(None) is None and ais_type(99) is None


def _block_with_aids(root, lat, lon):
    n = 2 ** vchart.BLOCK_Z
    import math
    bx = int((lon + 180) / 360 * n)
    by = int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n)
    b = vchart.LayerBuilder("point")
    x, y = vchart.to_block(bx, by)(*vchart.lonlat_to_mercator(lon, lat))
    b.point(("Merimajakka", 99, "K", "Rönnskär"), x, y)
    b.point(("Viitta", 1, "E", None), x + 10, y)  # unnamed: not indexed
    b.point(("Poiju", 2, "K", "Svartö S"), x + 20, y)
    levels = [{"aid": b.layer} for _ in vchart.LEVELS]
    root.mkdir(parents=True, exist_ok=True)
    vchart.write_block(root / f"{bx}_{by}.fwc", {"v": 2, "missing": []}, levels)
    vchart.write_block(root / f"{bx}_{by}.fw.fwc", {"v": 2, "missing": []}, levels)  # a part: skipped


def test_aid_index(tmp_path):
    _block_with_aids(tmp_path / "blocks", 59.9, 24.4)
    aids = AidNames(SimpleNamespace(dir=tmp_path))
    aids._scan_files()
    assert sorted(r[0] for r in aids.rows) == ["Rönnskär", "Svartö S"]
    ronn = next(r for r in aids.rows if r[0] == "Rönnskär")
    assert ronn[1] == "merimajakka" and ronn[4] == 1
    assert abs(ronn[2] - 59.9) < 1e-3 and abs(ronn[3] - 24.4) < 1e-3
    # Kept on disk, and found again without reading the block.
    again = AidNames(SimpleNamespace(dir=tmp_path))
    again._load()
    assert [r[0] for _, r in again.search(["ronnskar"], "ronnskar")] == ["Rönnskär"]


def test_reserve_names(tmp_path):
    (tmp_path / "r").mkdir()
    with gzip.open(tmp_path / "r" / "1_2.json.gz", "wt", encoding="utf-8") as f:
        json.dump({"reserves": [{"id": "w1", "name": "Porkkalan luonnonsuojelualue", "kind": "Nature reserve",
                                 "bbox": [24.3, 59.9, 24.5, 60.0]}, {"id": "w2", "name": None, "bbox": [1, 2, 3, 4]}]}, f)
    names = ReserveNames(SimpleNamespace(dir=tmp_path / "r"))
    got = asyncio.run(names.search(["porkkalan"], "porkkalan"))
    assert [r[1] for _, r in got] == ["Porkkalan luonnonsuojelualue"]


def test_finder_mixes_kinds(tmp_path):
    from ferrywatch.store import Store

    store = Store(tmp_path / "t.db")
    store.replace_vessels([(230184000, "SILJA SERENADE", "SILJA SERENADE", 60, 203, "FIHEL", 0, 8715259),
                           (230000999, "PORKKALA", "PORKKALA", 50, 15, "", 0, None)])
    _block_with_aids(tmp_path / "vc" / "blocks", 59.9, 24.4)

    class Src:
        def __init__(self, rows):
            self.rows = rows

        def search(self, q, lat, lon, limit):
            return {"harbours": [r for r in self.rows if fold(q) in fold(r[3])]}

    notices = SimpleNamespace(structures_cached=lambda: [
        {"kind": "bridge", "name": "Porkkalan silta", "at": [60.0, 24.4], "height_m": 4.5}])
    fw = SimpleNamespace(
        store=store, vchart=SimpleNamespace(dir=tmp_path / "vc"), reserves=SimpleNamespace(dir=tmp_path / "none"),
        harbours=Src([[1, 59.98, 24.42, "Porkkalan Marina", "Marina"]]), ashore=Src([]),
        stations=SimpleNamespace(stations={1: {"id": 1, "name": "Kirkkonummi Porkkala", "lat": 59.95, "lon": 24.38}},
                                 fetched=1.0),
        notices=notices)
    finder = Finder(fw)
    finder.aids._scan_files()

    async def run(q):
        return await finder.find(q, (60.0, 24.4))

    out = asyncio.run(run("porkkala"))
    kinds = {r["type"] for r in out["results"]}
    assert kinds == {"vessel", "harbour", "station", "structure"}
    assert out["results"][0]["name"] == "Porkkala" and out["results"][0]["detail"] == "Pilot · 15 m"
    bridge = next(r for r in out["results"] if r["type"] == "structure")
    assert bridge["label"] == "Opening bridge" and bridge["detail"] == "4.5 m clearance"
    imo = asyncio.run(run("8715259"))["results"]
    assert imo[0]["mmsi"] == 230184000 and imo[0]["score"] == 0 and "Passenger · 203 m" in imo[0]["detail"]
    aid = asyncio.run(run("rönnskär"))["results"]
    assert aid[0]["type"] == "aid" and aid[0]["label"] == "Lighthouse" and aid[0]["light"]
    assert asyncio.run(run("p"))["results"] == []
