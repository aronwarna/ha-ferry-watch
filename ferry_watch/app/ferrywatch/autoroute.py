"""Automatic route planning along the marked fairways, for the route tool. Planning only,
not for navigation.

The drawn chart's navigation lines (Väylävirasto's fairway centre lines, with each
fairway's design draught) become a graph: line vertices, the points where lines meet or
cross, and where a block edge cut a line, snapped together within a few metres. Lines too
shallow for the boat (design draught under its draught plus a margin) are left out. The
start and end join the nearest fairways with straight legs, A* finds the shortest way,
and the result is thinned to a few dozen points the user can then drag.

Only chart blocks already on disk count: blocks that aren't are downloaded in the
background and the answer says so. The graph is kept for a few minutes for re-planning in
the same area; it is a few flat arrays (well under a megabyte for a long route).
"""

from __future__ import annotations

import asyncio
import heapq
import logging
import math
import time
from array import array
from typing import Any

from .geo import distance_m
from .vchart import (BLOCK_Z, HALF, UNITS, block_bounds, clip_line, lonlat_to_mercator, mercator_to_lonlat,
                     near_finland, simplify)

_LOGGER = logging.getLogger(__name__)

NM = 1852.0
SNAP_M = 15  # vertices this close are one node; a line ending this close to another joins it
DRAUGHT_MARGIN = 0.3  # m under the keel beyond the boat's draught
CONNECT_M = 3 * NM  # straight legs from the start and end to the fairways, normally
CONNECT_MAX_M = 10 * NM  # beyond this the start or end is too far from any fairway
CONNECT_EDGES = 3  # fairway pieces the start and end may join
PAD_M = 6000  # around the start and end when choosing blocks...
PAD_MAX_M = 10000  # ...a little more on long routes, which may detour
MAX_BLOCKS = 60
BLOCK_WAIT = 8  # s to read one block that's on disk
CACHE_SECONDS = 300
MAX_POINTS = 60  # in the answer
SIMPLIFY_M = 20


class Graph:
    """Nodes (mercator x/y and lat/lon) and undirected edges in compressed rows: node u's
    neighbours are to[off[u]:off[u+1]], at distances w (metres)."""

    __slots__ = ("x", "y", "lat", "lon", "off", "to", "w")

    def __init__(self) -> None:
        self.x, self.y, self.lat, self.lon = array("d"), array("d"), array("d"), array("d")
        self.off, self.to, self.w = array("i", [0]), array("i"), array("f")

    @property
    def n(self) -> int:
        return len(self.x)

    def edges(self):
        for u in range(self.n):
            for k in range(self.off[u], self.off[u + 1]):
                if self.to[k] > u:
                    yield u, self.to[k]


def _metres_per_unit(y: float) -> float:
    """Ground metres per web-mercator metre at mercator y."""
    return math.cos(math.radians(mercator_to_lonlat(0, y)[1]))


def block_lines(layer: Any, bx: int, by: int, draught: float) -> list[list[tuple[float, float]]]:
    """A block's fairway lines deep enough for the draught, in mercator metres, clipped to
    the block itself (blocks keep a margin past their edges; clipped exactly, the halves
    of a line cut by a block edge meet again at the same point)."""
    minx, _, _, maxy = block_bounds(bx, by)
    scale = (2 * HALF / 2**BLOCK_Z) / UNITS
    out = []
    for i in range(layer.n):
        props = layer.props[layer.pi[i]]
        try:
            design = float(props[0]) if props and props[0] is not None else None
        except (TypeError, ValueError):
            design = None
        if design is not None and design < draught + DRAUGHT_MARGIN:
            continue
        for part in clip_line(layer.pts(layer.vo[i], layer.vo[i + 1]), 0, 0, UNITS, UNITS):
            out.append([(minx + px * scale, maxy - py * scale) for px, py in part])
    return out


def build_graph(lines: list[list[tuple[float, float]]]) -> Graph:
    """The fairway graph from lines in mercator metres (see the module docstring)."""
    g = Graph()
    segs = [(a[0], a[1], b[0], b[1]) for line in lines for a, b in zip(line, line[1:]) if a != b]
    if not segs:
        g.off = array("i", [0])
        return g
    k = _metres_per_unit(sum(s[1] for s in segs) / len(segs))
    snap = SNAP_M / k
    cell = max(800 / k, snap * 4)

    # Where each segment is cut: crossings, and other lines' ends that touch it.
    cuts: list[set[float]] = [{0.0, 1.0} for _ in segs]
    grid: dict[tuple[int, int], list[int]] = {}
    for i, (x1, y1, x2, y2) in enumerate(segs):
        for cx in range(math.floor((min(x1, x2) - snap) / cell), math.floor((max(x1, x2) + snap) / cell) + 1):
            for cy in range(math.floor((min(y1, y2) - snap) / cell), math.floor((max(y1, y2) + snap) / cell) + 1):
                grid.setdefault((cx, cy), []).append(i)

    def touch(i: int, px: float, py: float) -> None:
        x1, y1, x2, y2 = segs[i]
        dx, dy = x2 - x1, y2 - y1
        t = ((px - x1) * dx + (py - y1) * dy) / (dx * dx + dy * dy)
        if 0 < t < 1 and math.hypot(x1 + t * dx - px, y1 + t * dy - py) <= snap:
            cuts[i].add(round(t, 9))

    done: set[tuple[int, int]] = set()
    for members in grid.values():
        for a in range(len(members)):
            i = members[a]
            x1, y1, x2, y2 = segs[i]
            for b in range(a + 1, len(members)):
                j = members[b]
                if (i, j) in done:
                    continue
                done.add((i, j))
                x3, y3, x4, y4 = segs[j]
                d1x, d1y, d2x, d2y = x2 - x1, y2 - y1, x4 - x3, y4 - y3
                den = d1x * d2y - d1y * d2x
                if den:
                    t = ((x3 - x1) * d2y - (y3 - y1) * d2x) / den
                    u = ((x3 - x1) * d1y - (y3 - y1) * d1x) / den
                    if 0 < t < 1 and 0 < u < 1:
                        cuts[i].add(round(t, 9))
                        cuts[j].add(round(u, 9))
                touch(i, x3, y3)
                touch(i, x4, y4)
                touch(j, x1, y1)
                touch(j, x2, y2)
    del grid, done

    # Nodes, snapped together; edges between consecutive cut points.
    nodes: dict[tuple[int, int], list[int]] = {}

    def node(px: float, py: float) -> int:
        cx, cy = int(px // snap), int(py // snap)
        for ox in (-1, 0, 1):
            for oy in (-1, 0, 1):
                for n in nodes.get((cx + ox, cy + oy), ()):
                    if math.hypot(g.x[n] - px, g.y[n] - py) <= snap:
                        return n
        n = len(g.x)
        g.x.append(px)
        g.y.append(py)
        lon, lat = mercator_to_lonlat(px, py)
        g.lat.append(lat)
        g.lon.append(lon)
        nodes.setdefault((cx, cy), []).append(n)
        return n

    edges: dict[tuple[int, int], float] = {}
    for (x1, y1, x2, y2), ts in zip(segs, cuts):
        prev = None
        for t in sorted(ts):
            n = node(x1 + t * (x2 - x1), y1 + t * (y2 - y1))
            if prev is not None and prev != n:
                key = (min(prev, n), max(prev, n))
                if key not in edges:
                    edges[key] = distance_m(g.lat[prev], g.lon[prev], g.lat[n], g.lon[n])
            prev = n
    del segs, cuts, nodes

    count = [0] * (g.n + 1)
    for u, v in edges:
        count[u + 1] += 1
        count[v + 1] += 1
    for i in range(g.n):
        count[i + 1] += count[i]
    g.off = array("i", count)
    g.to = array("i", [0]) * count[-1]
    g.w = array("f", [0.0]) * count[-1]
    fill = list(count[:-1])
    for (u, v), w in edges.items():
        for a, b in ((u, v), (v, u)):
            g.to[fill[a]] = b
            g.w[fill[a]] = w
            fill[a] += 1
    return g


def _nearest_edges(g: Graph, x: float, y: float, limit: int) -> list[tuple[float, int, int, float, float]]:
    """The fairway pieces nearest a point: (ground metres, u, v, foot x, foot y)."""
    k = _metres_per_unit(y)
    found = []
    for u, v in g.edges():
        ax, ay, bx, by = g.x[u], g.y[u], g.x[v], g.y[v]
        dx, dy = bx - ax, by - ay
        len2 = dx * dx + dy * dy
        t = 0.0 if len2 == 0 else max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / len2))
        fx, fy = ax + t * dx, ay + t * dy
        found.append((math.hypot(fx - x, fy - y) * k, u, v, fx, fy))
    found.sort()
    return found[:limit]


def find_route(g: Graph, start: tuple[float, float], end: tuple[float, float]) -> dict[str, Any]:
    """The shortest way from start to end (lat, lon) along the fairways: {"points":
    [[lat, lon], ...], "metres": length} or {"error": why}."""
    if g.n == 0:
        return {"error": "There are no marked fairways around here deep enough for this draught."}
    lat = list(g.lat)
    lon = list(g.lon)
    extra: dict[int, list[tuple[int, float]]] = {}  # the start, the end and where they join

    def add_node(la: float, lo: float) -> int:
        lat.append(la)
        lon.append(lo)
        return len(lat) - 1

    def link(a: int, b: int) -> None:
        w = distance_m(lat[a], lon[a], lat[b], lon[b])
        extra.setdefault(a, []).append((b, w))
        extra.setdefault(b, []).append((a, w))

    s, e = add_node(*start), add_node(*end)
    feet: dict[tuple[int, int], list[int]] = {}
    warnings = []
    for point, which, label in ((start, s, "start"), (end, e, "end")):
        x, y = lonlat_to_mercator(point[1], point[0])
        near = _nearest_edges(g, x, y, CONNECT_EDGES)
        if not near or near[0][0] > CONNECT_MAX_M:
            return {"error": f"The {label} is more than {CONNECT_MAX_M / NM:.0f} NM from a marked fairway "
                             "deep enough for this draught."}
        if near[0][0] > CONNECT_M:
            warnings.append(f"The {label} is {near[0][0] / NM:.1f} NM from the nearest marked fairway: "
                            "check that straight leg on the chart.")
            near = near[:1]
        else:
            near = [n for n in near if n[0] <= CONNECT_M]
        for _, u, v, fx, fy in near:
            flon, flat = mercator_to_lonlat(fx, fy)
            foot = add_node(flat, flon)
            link(which, foot)
            link(foot, u)
            link(foot, v)
            for other in feet.get((u, v), ()):  # the start and end on the same piece
                link(foot, other)
            feet.setdefault((u, v), []).append(foot)

    # A* with the straight-line distance to the end as the estimate.
    best = {s: 0.0}
    prev: dict[int, int] = {}
    heap = [(distance_m(start[0], start[1], end[0], end[1]), 0.0, s)]
    elat, elon = end
    while heap:
        _, dist, u = heapq.heappop(heap)
        if u == e:
            break
        if dist > best.get(u, math.inf):
            continue
        nbrs = list(extra.get(u, ()))
        if u < g.n:
            nbrs += [(g.to[k], g.w[k]) for k in range(g.off[u], g.off[u + 1])]
        for v, w in nbrs:
            nd = dist + w
            if nd < best.get(v, math.inf):
                best[v] = nd
                prev[v] = u
                heapq.heappush(heap, (nd + distance_m(lat[v], lon[v], elat, elon), nd, v))
    if e not in best:
        return {"error": "No way along the marked fairways joins the start to the end."}
    path = [e]
    while path[-1] != s:
        path.append(prev[path[-1]])
    path.reverse()
    return {"points": simplify_path([(lat[n], lon[n]) for n in path]), "metres": best[e], "warnings": warnings}


def simplify_path(points: list[tuple[float, float]], max_points: int = MAX_POINTS) -> list[list[float]]:
    """Douglas-Peucker in mercator metres, loosened until the route has at most max_points."""
    merc = [lonlat_to_mercator(lo, la) for la, lo in points]
    k = _metres_per_unit(sum(p[1] for p in merc) / len(merc))
    tol = SIMPLIFY_M / k
    kept = simplify(merc, tol)
    while len(kept) > max_points:
        tol *= 1.5
        kept = simplify(merc, tol)
    out = []
    for x, y in kept:
        lo, la = mercator_to_lonlat(x, y)
        out.append([round(la, 6), round(lo, 6)])
    return out


def blocks_between(start: tuple[float, float], end: tuple[float, float]) -> list[tuple[int, int]]:
    """Chart blocks around both points (padded), that could hold Finnish fairways."""
    pts = [lonlat_to_mercator(lo, la) for la, lo in (start, end)]
    k = _metres_per_unit((pts[0][1] + pts[1][1]) / 2)
    span = math.hypot(pts[0][0] - pts[1][0], pts[0][1] - pts[1][1])
    pad = max(PAD_M / k, min(span * 0.15, PAD_MAX_M / k))
    size = 2 * HALF / 2**BLOCK_Z
    x0 = math.floor((min(p[0] for p in pts) - pad + HALF) / size)
    x1 = math.floor((max(p[0] for p in pts) + pad + HALF) / size)
    y0 = math.floor((HALF - max(p[1] for p in pts) - pad) / size)
    y1 = math.floor((HALF - min(p[1] for p in pts) + pad) / size)
    if (x1 - x0 + 1) * (y1 - y0 + 1) > MAX_BLOCKS * 4:
        return [(-1, -1)] * (MAX_BLOCKS + 1)  # far too big; not worth listing
    return [(bx, by) for by in range(y0, y1 + 1) for bx in range(x0, x1 + 1) if near_finland(block_bounds(bx, by))]


class AutoRouter:
    def __init__(self, vchart: Any):
        self.vchart = vchart
        self._cache: dict[tuple, tuple[float, Graph]] = {}
        self._downloading: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    def _on_disk(self, bx: int, by: int) -> bool:
        return all(self.vchart._path(bx, by, part).exists() for part in ("", "fw"))

    async def _download(self, keys: list[tuple[int, int]]) -> None:
        for key in keys:  # one at a time, as the chart itself does
            try:
                await self.vchart.block(*key)
            except Exception:  # noqa: BLE001 - a block that fails is just left out
                _LOGGER.debug("Chart block %s for route planning failed", key, exc_info=True)

    async def _graph(self, keys: list[tuple[int, int]], draught: float) -> Graph:
        cache_key = (tuple(keys), round(draught, 1))
        now = time.monotonic()
        self._cache = {k: v for k, v in self._cache.items() if now - v[0] < CACHE_SECONDS}
        if cache_key in self._cache:
            return self._cache[cache_key][1]
        lines: list[list[tuple[float, float]]] = []
        for bx, by in keys:  # one block at a time: only its fairway lines are kept
            try:
                layers = await asyncio.wait_for(self.vchart.block(bx, by), BLOCK_WAIT)
            except asyncio.TimeoutError:
                continue
            fline = (layers or {}).get("fline")
            if fline is not None:
                lines += block_lines(fline, bx, by, draught)
            del layers, fline
        graph = await asyncio.to_thread(build_graph, lines)
        del lines
        while len(self._cache) >= 2:
            self._cache.pop(next(iter(self._cache)))
        self._cache[cache_key] = (now, graph)
        return graph

    async def route(self, start: tuple[float, float], end: tuple[float, float], draught: float) -> dict[str, Any]:
        keys = blocks_between(start, end)
        if len(keys) > MAX_BLOCKS:
            return {"error": "Too far for automatic planning: plan it in parts of up to about 50 NM."}
        ready = [k for k in keys if self._on_disk(*k)]
        missing = [k for k in keys if k not in ready]
        if missing and (self._downloading is None or self._downloading.done()):
            self._downloading = asyncio.create_task(self._download(missing))
        async with self._lock:  # one graph built at a time keeps memory down
            graph = await self._graph(ready, draught)
            result = await asyncio.to_thread(find_route, graph, start, end)
        result.update({"blocks": len(keys), "downloading": len(missing)})
        if missing and "error" in result:
            result["error"] = (f"Chart data for {len(missing)} of the {len(keys)} areas along the way is "
                               "still downloading. Try again in a minute.")
        return result
