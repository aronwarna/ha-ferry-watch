"""Where Ferry Watch's memory goes, for Settings and the log.

The parts that hold data (chart blocks, depth model grids, the database's page cache,
ships, aircraft) are counted; the rest of the process's resident memory is Python itself,
the libraries and the code ("program"), and memory the allocator keeps for reuse.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import sys
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .__main__ import FerryWatch

CACHE_FOR = 30  # s a breakdown is reused (Settings asks for the state often)


def rss_mb() -> float | None:
    """This process's resident memory (VmRSS), in MB."""
    return _status("VmRSS")


def peak_mb() -> float | None:
    """The most it has had resident since it started (VmHWM), in MB."""
    return _status("VmHWM")


def _status(field: str) -> float | None:
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith(field + ":"):
                    return round(int(line.split()[1]) / 1024, 1)
    except (OSError, ValueError):
        pass
    return None


_sqlite_used: Any = False  # sqlite3_memory_used, once looked up (None if it can't be)


def sqlite_bytes() -> int | None:
    """What SQLite has allocated (its page caches and statements), from the library itself."""
    global _sqlite_used
    if _sqlite_used is False:
        _sqlite_used = None
        import _sqlite3  # the library is loaded already; dlopen hands back the same one
        for name in (getattr(_sqlite3, "__file__", None), ctypes.util.find_library("sqlite3"),
                     "libsqlite3.so.0", "libsqlite3.so"):
            if not name:
                continue
            try:
                fn = ctypes.CDLL(name).sqlite3_memory_used
            except (OSError, AttributeError):
                continue
            fn.restype = ctypes.c_int64
            fn.argtypes = []
            _sqlite_used = fn
            break
    return int(_sqlite_used()) if _sqlite_used else None


def deep_size(obj: Any, seen: set[int] | None = None, depth: int = 0) -> int:
    """Bytes held by an object and what it refers to (containers, dataclasses, slots)."""
    seen = set() if seen is None else seen
    if id(obj) in seen or depth > 12:
        return 0
    seen.add(id(obj))
    size = sys.getsizeof(obj)
    if isinstance(obj, (str, bytes, bytearray, int, float, bool)) or obj is None:
        return size
    if isinstance(obj, dict):
        for k, v in obj.items():
            size += deep_size(k, seen, depth + 1) + deep_size(v, seen, depth + 1)
    elif isinstance(obj, (list, tuple, set, frozenset)):
        for v in obj:
            size += deep_size(v, seen, depth + 1)
    else:
        if hasattr(obj, "__dict__"):
            size += deep_size(vars(obj), seen, depth + 1)
        for name in getattr(type(obj), "__slots__", ()):
            if hasattr(obj, name):
                size += deep_size(getattr(obj, name), seen, depth + 1)
    return size


_cached: tuple[float, dict[str, Any]] | None = None


def breakdown(fw: "FerryWatch") -> dict[str, Any]:
    """{"total": MB, "peak": MB, "parts": [[name, MB], ...]}, the rest being "program"."""
    global _cached
    now = time.monotonic()
    if _cached and now - _cached[0] < CACHE_FOR:
        return _cached[1]
    parts: list[tuple[str, float]] = []
    if fw.vchart:
        chart = fw.vchart.memory_bytes()
        parts += [("chart", chart["blocks"] + chart["heritage"]), ("depth model", chart["grids"])]
    db = sqlite_bytes()
    if db is not None:
        parts.append(("database", db))
    ships = deep_size(fw.tracker.ships) + deep_size(fw.fleet_tracker.ships)
    parts.append(("ships", ships))
    if fw.aircraft:
        parts.append(("aircraft", deep_size(fw.aircraft.aircraft)))
    total = rss_mb()
    counted = sum(b for _, b in parts) / 1e6
    result = {"total": total, "peak": peak_mb(),
              "parts": [[name, round(b / 1e6, 1)] for name, b in parts],
              "program": round(total - counted, 1) if total is not None else None}
    _cached = (now, result)
    return result
