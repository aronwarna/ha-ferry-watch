"""A plain-text report of how the app is doing, for Settings › "Copy diagnostics": the
version, memory, every data source's state and the latest warnings from the log, to paste
to whoever is helping. Keys, tokens and passwords are taken out."""

from __future__ import annotations

import collections
import json
import logging
import re
import time
from typing import Any

from . import __version__, config

LOG_LINES = 60
STARTED = time.time()
_SECRET = re.compile(r"(?i)((?:api[-_]?key|token|key|password|authorization)\s*[=:]\s*)([^&\s\"']+)")


class RingHandler(logging.Handler):
    """Keeps the latest warnings and errors in memory (a few kB)."""

    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.lines: collections.deque[str] = collections.deque(maxlen=LOG_LINES)
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.lines.append(self.format(record)[:600])
        except Exception:  # noqa: BLE001 - logging must never raise
            pass


RING = RingHandler()


def redact(text: str, secrets: list[str | None] = ()) -> str:
    for secret in secrets:
        if secret and len(secret) >= 4:
            text = text.replace(secret, "***")
    return _SECRET.sub(r"\1***", text)


def _flat(value: Any, limit: int = 400) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str, ensure_ascii=False)
    return text if len(text) <= limit else text[:limit] + "…"


def report(state: dict[str, Any], overlays: list[dict[str, Any]], secrets: list[str | None] = ()) -> str:
    now = time.time()
    up = now - STARTED
    mode = "own container" if config.STANDALONE else "dev" if config.DEV else "Home Assistant app"
    out = [
        f"Ferry Watch {__version__} ({mode}), {time.strftime('%Y-%m-%d %H:%M %Z', time.localtime(now))}",
        f"Running for {int(up // 3600)} h {int(up % 3600 // 60)} min, memory {state.get('memory_mb')} MB",
        f"Home Assistant: {'connected' if state.get('ha_connected') else 'not connected'}"
        f"{', ntfy set' if config.NTFY_URL else ''}",
        "",
        "Sources",
    ]
    for key in ("feed", "port_calls", "chart", "vchart", "satellite", "weather"):
        if state.get(key) is not None:
            out.append(f"- {key}: {_flat(state[key])}")
    air = state.get("aircraft") or {}
    out.append(f"- aircraft: enabled {air.get('enabled')}, error {air.get('error')}, flights {air.get('flights_stored')}")
    out += ["", "Weather layers"]
    for ov in overlays:
        facts = {k: v for k, v in ov.items()
                 if k in ("error", "check") and v not in (None, "", [], {})}
        times = ov.get("times")
        if isinstance(times, list):
            facts["times"] = len(times)
        out.append(f"- {ov.get('id')}: {_flat(facts) if facts else 'ok'}")
    memory = state.get("memory")
    if memory:
        out += ["", f"Memory: {_flat(memory, 800)}"]
    out += ["", f"Latest warnings ({len(RING.lines)})"]
    out += list(RING.lines) or ["(none since the start)"]
    return redact("\n".join(out), secrets)


def whats_new(changelog: str, version: str, since: str | None = None, limit: int = 5) -> list[dict[str, Any]]:
    """The changelog's sections from `version` back to (not including) `since`, newest
    first, at most `limit`: [{version, lines}], the lines as written (Markdown bullets)."""
    sections: list[dict[str, Any]] = []
    current = None
    for line in changelog.splitlines():
        if line.startswith("## "):
            current = {"version": line[3:].strip(), "lines": []}
            sections.append(current)
        elif current is not None and line.strip():
            current["lines"].append(line.rstrip())
    out = []
    started = False
    for section in sections:
        if section["version"] == version:
            started = True
        if not started:
            continue
        if since and section["version"] == since:
            break
        out.append(section)
        if len(out) >= limit or not since:
            break
    return out
