"""Made-up Sentinel-2 days and pictures for the simulator (recentsat.py, FW_SIMULATE=1).

A pass every two or three days at about 10 UTC, cloudiness from a fixed sequence; some
passes only cover the western part of any view (the edge of the satellite's swath runs
north-south through it), like the real ones do.
"""

from __future__ import annotations

import io
import time

CLOUDS = [12, 95, 100, 64, 3, 100, 88, 27, 100, 45, 9, 100, 76, 100, 18, 99, 52, 100, 6, 81]


def _days(days_back: int) -> list[tuple[str, int, bool]]:
    now = time.time()
    out = []
    i = 0
    t = now - 86400 * 0.6
    while t > now - days_back * 86400:
        day = time.strftime("%Y-%m-%d", time.gmtime(t))
        out.append((day, CLOUDS[i % len(CLOUDS)], i % 3 == 1))
        t -= 86400 * (2 if i % 2 else 3)
        i += 1
    return out


def fake_items(area, days_back: int) -> list[dict]:
    s, w, n, e = area
    items = []
    for day, cloud, half in _days(days_back):
        east = (w + e) / 2 + 0.1 if half else e + 1
        ring = [[w - 1, s - 1], [east, s - 1], [east + 0.2, n + 1], [w - 1, n + 1], [w - 1, s - 1]]
        items.append({"id": f"S2B_MSIL2A_{day.replace('-', '')}T100249_fake",
                      "geometry": {"type": "Polygon", "coordinates": [ring]},
                      "properties": {"datetime": f"{day}T10:02:49Z", "eo:cloud_cover": cloud}})
    return items


def fake_tile(day: str, z: int, x: int, y: int) -> bytes:
    from PIL import Image, ImageDraw

    seed = sum(map(ord, day))
    hue = seed % 60
    img = Image.new("RGBA", (256, 256), (40 + hue, 80 + hue // 2, 110, 255))
    d = ImageDraw.Draw(img)
    # "land": blobs placed by tile position, the same every day
    for k in range(6):
        cx = (x * 37 + k * 91) % 256
        cy = (y * 53 + k * 67) % 256
        r = 18 + (k * 13 + x + y) % 30
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(70 + hue, 100, 60, 255))
    # "clouds": by the day's cloudiness
    cloud = next((c for dd, c, _ in _days(70) if dd == day), 50)
    for k in range(cloud // 8):
        cx = (seed * 7 + k * 113 + x * 29) % 256
        cy = (seed * 3 + k * 71 + y * 31) % 256
        d.ellipse([cx - 30, cy - 20, cx + 30, cy + 20], fill=(235, 238, 242, 230))
    d.text((8, 8), f"{day} z{z}", fill=(255, 255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, "WEBP", quality=70)
    return buf.getvalue()

