"""Asking OpenStreetMap's Overpass API without leaning on one public server.

The main server (overpass-api.de) is often "at capacity" (502, 504, 429). So a query is
tried on each mirror in turn, twice round with a pause in between, and the answer of the
first that works is used. Errors are turned into a few plain words for the panel; the raw
text goes to the log only.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

import aiohttp

from .config import USER_AGENT

_LOGGER = logging.getLogger(__name__)

MIRRORS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
)
ROUNDS = 2
PAUSE = 8  # s before the second round
BUSY = (429, 502, 503, 504)
_last_good = 0  # index of the mirror that answered last: asked first next time


class OverpassError(ValueError):
    """Every mirror failed. str() is short and plain; .detail has the last raw error."""

    def __init__(self, plain: str, detail: str):
        super().__init__(plain)
        self.detail = detail


def plain_error(err: BaseException | str | None) -> str:
    """A raw error (an exception or its text) as a few plain words for the panel."""
    if not err:
        return ""
    text = str(err) or type(err).__name__
    low = text.lower()
    if isinstance(err, OverpassError):
        return text
    if isinstance(err, asyncio.TimeoutError) or "timeout" in low and "gateway" not in low:
        return "it took too long to answer"
    if re.search(r"\b(429|502|503|504)\b", text) or "capacity" in low or "too many" in low or "gateway" in low:
        return "its servers are busy"
    if "too big" in low:
        return "the answer was too big"
    if isinstance(err, aiohttp.ClientConnectionError) or "connect" in low or "dns" in low or "ssl" in low:
        return "no connection to it"
    if re.search(r"\b[45]\d\d\b", text):
        return "it answered with an error"
    return "it couldn't be read"


async def fetch(session: aiohttp.ClientSession, query: str, timeout: float = 180,
                max_bytes: int = 32 * 1024 * 1024, mirrors: tuple[str, ...] = MIRRORS,
                rounds: int = ROUNDS, pause: float = PAUSE) -> Any:
    """The JSON answer to an Overpass query, from the first mirror that gives one."""
    global _last_good
    order = list(mirrors[_last_good:] + mirrors[:_last_good]) if _last_good < len(mirrors) else list(mirrors)
    last: BaseException | None = None
    for attempt in range(rounds):
        if attempt:
            await asyncio.sleep(pause * attempt)
        for url in order:
            try:
                async with session.post(url, data={"data": query}, headers={"User-Agent": USER_AGENT},
                                        timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                    if resp.status != 200:
                        text = (await resp.content.read(300)).decode("utf-8", "replace")
                        raise ValueError(f"HTTP {resp.status}: {re.sub(r'<[^>]+>', ' ', text).strip()[:160]}")
                    chunks, size = [], 0
                    async for chunk in resp.content.iter_chunked(65536):
                        size += len(chunk)
                        if size > max_bytes:
                            raise ValueError("answer too big")
                        chunks.append(chunk)
                data = json.loads(b"".join(chunks))
                if isinstance(data, dict) and "elements" not in data and data.get("remark"):
                    raise ValueError(f"remark: {str(data['remark'])[:160]}")
                _last_good = mirrors.index(url) if url in mirrors else 0
                return data
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, OSError) as err:
                last = err
                _LOGGER.debug("Overpass %s: %s", url.split("/")[2], str(err) or type(err).__name__)
                if "too big" in str(err):
                    break  # the same answer is as big everywhere
    raise OverpassError(plain_error(last), f"{str(last) or type(last).__name__}")
