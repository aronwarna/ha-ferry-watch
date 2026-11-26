"""Notices when the app's main loop is held up, and by what.

Everything the panel asks for (ships, tiles, stats) is answered by one loop. If some piece
of work runs on it for long, every other answer waits: the map feels stuck. A small
watcher thread pokes the loop four times a second; when a poke is not answered within
STALL_S it looks at what the loop is doing right then and counts that place in the code.
Settings › Copy diagnostics lists the worst places, so a slow spot on the NAS can be found
without guessing. It costs one sleeping thread and a few kB.
"""

from __future__ import annotations

import asyncio
import collections
import logging
import sys
import threading
import time
from pathlib import Path
from typing import Any

_LOGGER = logging.getLogger(__name__)

POKE_S = 0.25
STALL_S = 0.3  # the loop not answering for this long counts as a stall
LOG_S = 2.0  # stalls this long are also written to the log
KEEP = 40  # places remembered
_PKG = Path(__file__).parent


class StallWatch:
    def __init__(self) -> None:
        self.roots: tuple[Path, ...] = (_PKG,)  # whose code counts as the app's own
        self.loop: asyncio.AbstractEventLoop | None = None
        self.main_id: int | None = None
        self.places: dict[str, list[float]] = {}  # place -> [count, total s, worst s, last ts]
        self.recent: collections.deque[tuple[float, float, str]] = collections.deque(maxlen=20)
        self._answered = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        if self._thread:
            return
        self.loop, self.main_id = loop, threading.get_ident()
        self._thread = threading.Thread(target=self._run, name="stall-watch", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _where(self) -> str:
        """The innermost line of this app's own code the loop is running now."""
        frame = sys._current_frames().get(self.main_id)  # noqa: SLF001 - that is what it is for
        chain: list[str] = []
        while frame is not None and len(chain) < 2:
            path = Path(frame.f_code.co_filename)
            if any(r in path.parents for r in self.roots) and path.name != "stalls.py":
                chain.append(f"{path.name}:{frame.f_lineno} {frame.f_code.co_name}")
            frame = frame.f_back
        return " < ".join(chain) or "outside the app (library or interpreter)"

    def _run(self) -> None:
        while not self._stop.wait(POKE_S):
            loop = self.loop
            if loop is None or loop.is_closed():
                return
            self._answered.clear()
            t0 = time.monotonic()
            try:
                loop.call_soon_threadsafe(self._answered.set)
            except RuntimeError:  # loop closed
                return
            if self._answered.wait(STALL_S):
                continue
            place = self._where()  # what holds it now, seen once it is already late
            self._answered.wait(30)
            took = time.monotonic() - t0
            row = self.places.get(place)
            if row is None:
                if len(self.places) >= KEEP:  # forget the least troublesome place
                    del self.places[min(self.places, key=lambda p: self.places[p][1])]
                row = self.places[place] = [0, 0.0, 0.0, 0.0]
            row[0] += 1
            row[1] += took
            row[2] = max(row[2], took)
            row[3] = time.time()
            self.recent.append((time.time(), took, place))
            if took >= LOG_S:
                _LOGGER.warning("The app was held up for %.1f s by %s", took, place)

    def summary(self, top: int = 8) -> dict[str, Any]:
        rows = sorted(self.places.items(), key=lambda kv: -kv[1][1])[:top]
        return {
            "stalls": int(sum(r[0] for r in self.places.values())),
            "seconds": round(sum(r[1] for r in self.places.values()), 1),
            "places": [{"where": p, "times": int(r[0]), "seconds": round(r[1], 2), "worst": round(r[2], 2)}
                       for p, r in rows],
        }


WATCH = StallWatch()
