"""One search for everything the app knows: ships (the vessel directory, by name, MMSI or
IMO), harbours, places to go ashore, named lights and marks from the drawn chart, nature
reserves, weather stations, and opening bridges, locks and canals.

The map's search box (static/whereami.js) asks /api/find as you type, and adds what the
panel itself knows (your tracked ships, pins, saved routes, layers) and place names from
OpenStreetMap (/api/places, slower: it goes out to Nominatim). Every row says what kind of
thing it is, so the panel can show an icon and a label with it.

Nothing here downloads anything: each source answers from what is already on the NAS. Two
small indexes are kept: the named lights and marks of the chart blocks downloaded so far
(scanned in the background, the first time a few minutes, then only blocks that changed;
on disk as vchart/aidnames.json.gz) and the reserves' names (from their cached cells).
"""
from __future__ import annotations

import asyncio
import gzip
import json
import logging
import math
import re
import time
import unicodedata
from pathlib import Path
from typing import Any

_LOGGER = logging.getLogger(__name__)

INDEX_REV = 1
RESCAN = 6 * 3600  # look for new or changed chart blocks this often (when someone searches)
RECHECK = 120  # and for blocks written since, at most this often
RESERVE_RESCAN = 60
PER_TYPE = 6
VESSELS = 8
BLOCK_NAME = re.compile(r"^(\d+)_(\d+)\.fwc$")  # a block's main part (depths, aids, …)

# Traficom's aid types (turvalaitetyyppifi) in English, as the panel labels them.
AID_LABELS = {
    "viitta": "Spar buoy", "poiju": "Buoy", "reunamerkki": "Edge mark", "linjamerkki": "Leading mark",
    "kummeli": "Cairn", "sektoriloisto": "Sector light", "merimajakka": "Lighthouse",
    "tunnusmajakka": "Beacon tower", "suuntaloisto": "Directional light", "apuloisto": "Light",
    "tutkamerkki": "Radar mark", "tutkamajakka": "Radar beacon", "jääpoiju": "Ice buoy",
}
LIGHTS = ("sektoriloisto", "merimajakka", "suuntaloisto", "apuloisto", "tunnusmajakka")
STRUCTURE_LABELS = {"bridge": "Opening bridge", "lock": "Lock", "canal": "Canal"}


def fold(text: str | None) -> str:
    """Lower case without accents (Rönnskär -> ronnskar), single spaces."""
    t = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().casefold()
    return " ".join(re.sub(r"[^\w]+", " ", t).split())


def score(name: str | None, words: list[str], whole: str) -> int | None:
    """How well a name matches the query's words (all must be in it): 0 the same, 1 starts
    with it, 2 a word starts with it, 3 somewhere inside; None for no match."""
    n = fold(name)
    if not n or not all(w in n for w in words):
        return None
    if n == whole:
        return 0
    if n.startswith(whole):
        return 1
    if any(part.startswith(words[0]) for part in n.split()):
        return 2
    return 3


def ais_type(code: Any) -> str | None:
    """An AIS ship type number in a word or two (as the panel's aisType)."""
    if not isinstance(code, int):
        return None
    named = {30: "Fishing", 31: "Towing", 32: "Towing", 33: "Dredger", 34: "Diving", 35: "Military",
             36: "Sailing", 37: "Pleasure craft", 50: "Pilot", 51: "Rescue", 52: "Tug", 53: "Port tender",
             55: "Law enforcement", 58: "Medical"}
    if code in named:
        return named[code]
    for lo, word in ((40, "High-speed craft"), (60, "Passenger"), (70, "Cargo"), (80, "Tanker")):
        if lo <= code < lo + 10:
            return word
    return None


def _km(lat: float, lon: float, near: tuple[float, float] | None) -> float | None:
    if not near:
        return None
    kx = math.cos(math.radians(near[0]))
    return math.hypot(lat - near[0], (lon - near[1]) * kx) * 111.2


class AidNames:
    """The named lights and marks in the chart blocks on disk: [name, label, lat, lon, lit]."""

    def __init__(self, vchart: Any):
        self.vchart = vchart
        self.path = Path(vchart.dir) / "aidnames.json.gz"
        self.rows: list[list[Any]] | None = None
        self.blocks: dict[str, list[Any]] = {}  # block file name -> [mtime, rows]
        self.scanned = 0.0
        self.total = 0
        self.done = 0
        self._task: asyncio.Task | None = None

    @property
    def busy(self) -> bool:
        return bool(self._task and not self._task.done())

    def _load(self) -> None:
        try:
            saved = json.loads(gzip.decompress(self.path.read_bytes()))
            if saved.get("rev") == INDEX_REV:
                self.blocks = saved["blocks"]
                self.scanned = float(saved.get("scanned", 0))
        except FileNotFoundError:
            pass
        except (OSError, ValueError, KeyError, TypeError) as err:
            _LOGGER.warning("Could not read %s: %s", self.path, err)
        self._flatten()

    def _flatten(self) -> None:
        self.rows = [r for _, rows in self.blocks.values() for r in rows]

    def ensure(self) -> None:
        """Loaded, and a scan started when one is due."""
        if self.rows is None:
            self._load()
        now = time.time()
        if self.busy or now - self.scanned < RECHECK:
            return
        if now - self.scanned > RESCAN or self._dir_changed():
            self._task = asyncio.create_task(self._scan())

    def _dir_changed(self) -> bool:
        """New blocks since the last scan (the folder changes when one is written)."""
        try:
            return (Path(self.vchart.dir) / "blocks").stat().st_mtime > self.scanned
        except OSError:
            return False

    async def _scan(self) -> None:
        try:
            await asyncio.to_thread(self._scan_files)
        except Exception:  # noqa: BLE001 - a broken index must not break search
            _LOGGER.exception("Could not index the chart's lights and marks")

    def _scan_files(self) -> None:
        from . import vchart as vc

        root = Path(self.vchart.dir) / "blocks"
        files = sorted(p for p in (root.iterdir() if root.exists() else []) if BLOCK_NAME.match(p.name))
        self.total, self.done = len(files), 0
        names = {p.name for p in files}
        changed = False
        for gone in [k for k in self.blocks if k not in names]:
            del self.blocks[gone]
            changed = True
        level = len(vc.LEVELS) - 1  # the full detail: every aid, not only the big ones
        for p in files:
            self.done += 1
            try:
                mtime = int(p.stat().st_mtime)
            except OSError:
                continue
            if self.blocks.get(p.name, [None])[0] == mtime:
                continue
            m = BLOCK_NAME.match(p.name)
            bx, by = int(m.group(1)), int(m.group(2))
            rows: list[list[Any]] = []
            try:
                aid = vc.read_block(p, level, ("aid",))[1].get("aid")
            except (OSError, ValueError, EOFError) as err:
                _LOGGER.debug("Skipping %s for the aid index: %s", p.name, err)
                aid = None
            if aid is not None and aid.n:
                minx, _, _, maxy = vc.block_bounds(bx, by)
                scale = vc.UNITS / (2 * vc.HALF / 2**vc.BLOCK_Z)
                for i in range(aid.n):
                    props = tuple(aid.props[aid.pi[i]]) + (None,) * 4
                    name = props[3]
                    if not isinstance(name, str) or not name.strip():
                        continue
                    kind = str(props[0] or "").lower()
                    x = minx + aid.co[2 * i] / scale
                    y = maxy - aid.co[2 * i + 1] / scale
                    lon, lat = vc.mercator_to_lonlat(x, y)
                    rows.append([name.strip(), kind, round(lat, 5), round(lon, 5), 1 if props[2] == "K" else 0])
            self.blocks[p.name] = [mtime, rows]
            changed = True
            self._flatten()  # searchable as it goes
            time.sleep(0.005)  # leave the CPU to the rest of the app
        self.scanned = time.time()
        self._flatten()
        if changed or not self.path.exists():
            body = gzip.compress(json.dumps({"rev": INDEX_REV, "scanned": self.scanned, "blocks": self.blocks},
                                            separators=(",", ":"), ensure_ascii=False).encode())
            tmp = self.path.with_suffix(".tmp")
            tmp.write_bytes(body)
            tmp.replace(self.path)
        _LOGGER.info("Chart lights and marks indexed for search: %s named in %s blocks",
                     len(self.rows or []), len(files))

    def search(self, words: list[str], whole: str) -> list[tuple[int, list[Any]]]:
        self.ensure()
        out = []
        seen = set()
        for r in self.rows or []:
            s = score(r[0], words, whole)
            if s is None:
                continue
            key = (r[0], round(r[2], 3), round(r[3], 3))  # the same aid in two blocks' margins
            if key in seen:
                continue
            seen.add(key)
            out.append((s, r))
        return out


class ReserveNames:
    """Names of the nature reserves in the cells already downloaded (reserves.py)."""

    def __init__(self, reserves: Any):
        self.reserves = reserves
        self.cells: dict[str, tuple[int, list[list[Any]]]] = {}
        self.checked = 0.0

    def _scan(self) -> None:
        root = Path(self.reserves.dir)
        files = list(root.glob("*.json.gz")) if root.exists() else []
        names = {p.name for p in files}
        for gone in [k for k in self.cells if k not in names]:
            del self.cells[gone]
        for p in files:
            try:
                mtime = int(p.stat().st_mtime)
            except OSError:
                continue
            if self.cells.get(p.name, (None,))[0] == mtime:
                continue
            rows = []
            try:
                with gzip.open(p, "rt", encoding="utf-8") as f:
                    for r in json.load(f).get("reserves") or []:
                        if r.get("name") and r.get("bbox"):
                            rows.append([r["id"], r["name"], r.get("kind") or "Nature reserve", r["bbox"]])
            except (OSError, ValueError, AttributeError) as err:
                _LOGGER.debug("Skipping %s for reserve search: %s", p, err)
            self.cells[p.name] = (mtime, rows)
        self.checked = time.time()

    async def search(self, words: list[str], whole: str) -> list[tuple[int, list[Any]]]:
        if time.time() - self.checked > RESERVE_RESCAN:
            await asyncio.to_thread(self._scan)
        out, seen = [], set()
        for _, rows in self.cells.values():
            for r in rows:
                if r[0] in seen:
                    continue
                s = score(r[1], words, whole)
                if s is not None:
                    seen.add(r[0])
                    out.append((s, r))
        return out


class Finder:
    def __init__(self, fw: Any):
        self.fw = fw
        self.aids = AidNames(fw.vchart)
        self.reserves = ReserveNames(fw.reserves)
        self._stations_task: asyncio.Task | None = None

    def _vessels(self, q: str, words: list[str], whole: str) -> list[dict[str, Any]]:
        from .ais import display
        from .fleets import normalize

        store = self.fw.store
        digits = q.replace(" ", "")
        rows = store.search_vessels(normalize(q), VESSELS) if not digits.isdigit() or len(digits) >= 3 else []
        if digits.isdigit() and len(digits) == 7:  # an IMO number
            have = {r["mmsi"] for r in rows}
            rows = [r for r in store.vessels_by_imo(int(digits)) if r["mmsi"] not in have] + rows
        tracked = {s["mmsi"] for s in store.ships()}
        out = []
        for r in rows:
            name = display(r["name"]) or f"MMSI {r['mmsi']}"
            s = score(name, words, whole)
            if s is None:  # matched by number
                s = 0 if digits in (str(r["mmsi"]), str(r.get("imo") or "")) else 1
            facts = [ais_type(r["type"]), f"{r['length']} m" if r["length"] else None,
                     f"to {r['destination'].strip().title()}" if (r["destination"] or "").strip() else None]
            out.append({"type": "vessel", "name": name, "detail": " · ".join(f for f in facts if f), "mmsi": r["mmsi"], "imo": r.get("imo"),
                        "ship_type": r["type"], "length": r["length"], "destination": r["destination"],
                        "tracked": r["mmsi"] in tracked, "score": s})
        return out

    def _stations(self, words: list[str], whole: str) -> list[dict[str, Any]]:
        st = self.fw.stations
        if not st.stations and not st.fetched and not (self._stations_task and not self._stations_task.done()):
            self._stations_task = asyncio.create_task(st.latest())  # ready for the next letters
        out = []
        for s in st.stations.values():
            sc = score(s.get("name"), words, whole)
            if sc is not None:
                out.append({"type": "station", "name": s["name"], "id": s["id"], "lat": s["lat"], "lon": s["lon"],
                            "score": sc})
        return out

    def _structures(self, words: list[str], whole: str) -> list[dict[str, Any]]:
        out = []
        for s in self.fw.notices.structures_cached():
            sc = score(s.get("name"), words, whole)
            if sc is None or not s.get("at"):
                continue
            facts = [f"{s[k]:g} m {w}" for k, w in (("height_m", "clearance"), ("width_m", "wide"), ("depth_m", "deep"))
                     if isinstance(s.get(k), (int, float))]
            out.append({"type": "structure", "kind": s["kind"], "label": STRUCTURE_LABELS.get(s["kind"], "Structure"),
                        "name": s["name"], "lat": s["at"][0], "lon": s["at"][1], "score": sc,
                        "detail": ", ".join(facts)})
        return out

    def _harbours(self, src: Any, kind: str, q: str, near: tuple[float, float]) -> list[dict[str, Any]]:
        words = fold(q).split()
        whole = " ".join(words)
        out = []
        for r in src.search(q, near[0], near[1], PER_TYPE * 2).get("harbours", []):
            sc = score(r[3], words, whole)
            out.append({"type": kind, "name": r[3], "label": r[4] if kind == "ashore" else None,
                         "detail": r[4] if kind == "harbour" else None,
                         "lat": r[1], "lon": r[2], "score": 3 if sc is None else sc})
        return out

    async def find(self, q: str, near: tuple[float, float] | None) -> dict[str, Any]:
        q = q.strip()[:60]
        words = fold(q).split()[:5]
        whole = " ".join(words)
        if len(whole) < 2:
            return {"results": [], "indexing": None}
        here = near or (60.1, 24.9)
        rows: list[dict[str, Any]] = []
        rows += self._vessels(q, words, whole)
        rows += self._harbours(self.fw.harbours, "harbour", q, here)
        rows += self._harbours(self.fw.ashore, "ashore", q, here)
        for s, r in self.aids.search(words, whole):
            kind = r[1]
            label = AID_LABELS.get(kind, "Mark")
            rows.append({"type": "aid", "name": r[0], "label": label, "lit": bool(r[4]),
                         "light": kind in LIGHTS, "lat": r[2], "lon": r[3], "score": s,
                         "detail": "Lit" if r[4] and kind not in LIGHTS else None})
        for s, r in await self.reserves.search(words, whole):
            b = r[3]
            rows.append({"type": "reserve", "id": r[0], "name": r[1], "label": r[2], "bbox": b,
                         "lat": (b[1] + b[3]) / 2, "lon": (b[0] + b[2]) / 2, "score": s})
        rows += self._stations(words, whole)
        rows += self._structures(words, whole)
        for r in rows:
            if r.get("lat") is not None:
                r["km"] = round(_km(r["lat"], r["lon"], near), 1) if near else None
        rows.sort(key=lambda r: (r["score"], r.get("km") if r.get("km") is not None else 1e9))
        # A few of each kind, the best first, so one kind never crowds out the rest.
        counts: dict[str, int] = {}
        out = []
        for r in rows:
            cap = VESSELS if r["type"] == "vessel" else PER_TYPE
            if counts.get(r["type"], 0) >= cap:
                continue
            counts[r["type"]] = counts.get(r["type"], 0) + 1
            out.append(r)
        indexing = {"done": self.aids.done, "total": self.aids.total} if self.aids.busy else None
        return {"results": out, "indexing": indexing}
