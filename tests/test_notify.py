import asyncio
import io
import time
from types import SimpleNamespace

from PIL import Image

import ferrywatch.config as config
import ferrywatch.notify as notify
from ferrywatch.config import Options
from ferrywatch.mapimage import choose_zoom, render
from ferrywatch.simulate import FakeHA
from ferrywatch.store import Store
from ferrywatch.tracker import Tracker

MMSI = 230184000
AREA = [[60.2, 24.3], [60.2, 24.6], [60.0, 24.6], [60.0, 24.3]]


def blank_tile_fetcher(session=None):
    buf = io.BytesIO()
    Image.new("RGB", (256, 256), (200, 220, 230)).save(buf, "PNG")
    data = buf.getvalue()

    async def fetch(z, x, y):
        return data
    return fetch


def make_fw(tmp_path, monkeypatch, phone=(60.115, 24.45), zones=True):
    monkeypatch.setattr(config, "MEDIA_DIR", tmp_path / "media" / "ferry_watch")
    monkeypatch.setattr(notify, "http_tile_fetcher", blank_tile_fetcher)
    store = Store(tmp_path / "t.db")
    store.add_ship(MMSI, "Silja Serenade", "#2563eb")
    if zones:
        store.add_zone("Me", "phone", "polygon", AREA)
    tracker = Tracker(store)
    tracker.load()
    now = int(time.time())
    for i in range(10):
        tracker.on_location(MMSI, {"time": now - 600 + i * 60, "lat": 60.1, "lon": 24.6 - i * 0.02,
                                   "sog": 17.0, "cog": 260.0, "heading": 260})
    fw = SimpleNamespace(store=store, tracker=tracker, ha=FakeHA(phone), options=Options(),
                         photos=SimpleNamespace(remote_url=lambda m: "https://img.example/s.jpg"),
                         session=None)
    return fw, notify.Notifier(fw)


def passage(store, **kw):
    p = {"mmsi": MMSI, "ts": int(time.time()), "route": "inner", "direction": "west",
         "zone_id": 1, "lat": 60.1, "lon": 24.42, "sog": 17.0, **kw}
    p["id"] = store.add_passage(p)
    return p


def test_inner_passage_with_phone_in_area_is_sent(tmp_path, monkeypatch):
    fw, n = make_fw(tmp_path, monkeypatch)
    p = passage(fw.store)
    asyncio.run(n.handle(p))
    service, data = fw.ha.calls[-1]
    assert service == "notify.mobile_app_samsung_galaxy_s22"
    assert data["title"] == "Silja Serenade: inner route"
    assert data["message"].startswith("Towards Stockholm, 17 kn.")
    assert data["data"]["image"] == f"/media/local/ferry_watch/passage-{p['id']}.png"
    assert data["data"]["icon_url"] == "https://img.example/s.jpg"
    assert (config.MEDIA_DIR / f"passage-{p['id']}.png").exists()
    assert fw.store.passages()[0]["notified"] == 1
    assert fw.store.kv_get("last_notification")["mmsi"] == MMSI


def test_phone_outside_area_is_not_sent(tmp_path, monkeypatch):
    fw, n = make_fw(tmp_path, monkeypatch, phone=(60.4, 25.5))
    p = passage(fw.store)
    asyncio.run(n.handle(p))
    assert fw.ha.calls == []
    row = fw.store.passages()[0]
    assert row["notified"] == 0 and row["note"] == "phone outside your area"


def test_outer_and_late_passages_are_not_sent(tmp_path, monkeypatch):
    fw, n = make_fw(tmp_path, monkeypatch)
    asyncio.run(n.handle(passage(fw.store, route="outer")))
    asyncio.run(n.handle(passage(fw.store, ts=int(time.time()) - 3600)))
    assert fw.ha.calls == []
    notes = {r["note"] for r in fw.store.passages()}
    assert notes == {"outer route", "detected late (old data)"}


def test_no_area_drawn_means_always_notify(tmp_path, monkeypatch):
    fw, n = make_fw(tmp_path, monkeypatch, phone=(60.4, 25.5), zones=False)
    asyncio.run(n.handle(passage(fw.store)))
    assert len(fw.ha.calls) == 1


def test_eta_from_earlier_passage(tmp_path, monkeypatch):
    fw, n = make_fw(tmp_path, monkeypatch)
    # An earlier westbound passage whose track passes the phone 12 minutes after the gate.
    t0 = 1_000_000
    other = 230361000
    fw.store.add_ship(other, "Viking Gabriella", "#ea580c")
    fw.tracker.load()
    for i in range(30):
        fw.tracker.on_location(other, {"time": t0 + i * 60, "lat": 60.1, "lon": 24.42 - i * 0.01,
                                       "sog": 17.0, "cog": 270.0})
    fw.store.add_passage({"mmsi": other, "ts": t0, "route": "inner", "direction": "west", "zone_id": 1,
                          "lat": 60.1, "lon": 24.42, "sog": 17.0})
    title, message = n.build_message(passage(fw.store), (60.105, 24.30))
    assert "Passes nearest you in about 12 min" in message


def test_render_map_picture():
    pts = [(60.1, 24.6 - i * 0.02) for i in range(10)]
    png = asyncio.run(render(pts, (60.1, 24.4, 260), "#2563eb", blank_tile_fetcher(), extra=[(60.115, 24.45)]))
    img = Image.open(io.BytesIO(png))
    assert img.size == (800, 400)
    assert 9 <= choose_zoom(pts + [(60.1, 24.4)], 800, 400, 50) <= 13


def test_passage_event_and_last_inner_sensor(tmp_path, monkeypatch):
    fw, n = make_fw(tmp_path, monkeypatch, phone=(60.4, 25.5))  # outside the area: no alert
    p = passage(fw.store)
    asyncio.run(n.handle(p))
    event, data = fw.ha.events[-1]
    assert event == "ferry_watch_passage"
    assert data["ship"] == "Silja Serenade" and data["route"] == "inner"
    assert data["direction"] == "towards Stockholm"
    assert data["alert_sent"] is False and data["alert_note"] == "phone outside your area"
    state, attrs = fw.ha.states[notify.LAST_INNER_ENTITY]
    assert state == data["time"] and attrs["device_class"] == "timestamp"

    outer = passage(fw.store, route="outer", direction="east")
    asyncio.run(n.handle(outer))
    assert fw.ha.events[-1][1]["route"] == "outer"
    assert fw.ha.states[notify.LAST_INNER_ENTITY][0] == state  # unchanged by outer passages

    # After a Home Assistant restart the sensor is rebuilt from the database.
    fw.ha.states.clear()
    asyncio.run(n.publish_last_inner())
    assert fw.ha.states[notify.LAST_INNER_ENTITY][1]["ship"] == "Silja Serenade"


def test_feed_warnings_only_by_day():
    import datetime as dt
    day = dt.datetime(2026, 10, 18, 9, 0).timestamp()
    night = dt.datetime(2026, 10, 18, 3, 0).timestamp()
    assert notify.feed_warning_hours(day)
    assert not notify.feed_warning_hours(night)
