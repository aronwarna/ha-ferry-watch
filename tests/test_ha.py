import asyncio
from types import SimpleNamespace

import aiohttp
from aiohttp import web

import ferrywatch.config as config
import ferrywatch.ha as ha
from ferrywatch.store import Store
from ferrywatch.tracker import Tracker


def test_entity_id():
    assert ha.entity_id("Silja Serenade") == "device_tracker.ferry_watch_silja_serenade"
    assert ha.entity_id("Viking  Gabriella!") == "device_tracker.ferry_watch_viking_gabriella"


def test_publish_posts_device_tracker_state(tmp_path, monkeypatch):
    received = []

    async def handler(request):
        received.append((request.match_info["entity"], request.headers["Authorization"], await request.json()))
        return web.json_response({})

    async def run():
        app = web.Application()
        app.router.add_post("/api/states/{entity}", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        monkeypatch.setattr(config, "HA_API", f"http://127.0.0.1:{port}/api")

        store = Store(tmp_path / "t.db")
        store.add_ship(230184000, "Silja Serenade", "#000")
        tracker = Tracker(store)
        tracker.load()
        tracker.on_location(230184000, {"time": 1000, "lat": 60.1, "lon": 24.9, "sog": 15.0, "cog": 250.0})
        photos = SimpleNamespace(remote_url=lambda mmsi: "https://upload.example/ship.jpg")
        fw = SimpleNamespace(store=store, tracker=tracker, photos=photos)
        async with aiohttp.ClientSession() as session:
            await ha.HAPublisher(ha.HAClient(session, "token123"), 60, fw).publish(230184000)
        await runner.cleanup()

    asyncio.run(run())
    entity, auth, body = received[0]
    assert entity == "device_tracker.ferry_watch_silja_serenade"
    assert auth == "Bearer token123"
    assert body["state"] == "under_way"
    assert body["attributes"]["latitude"] == 60.1
    assert body["attributes"]["entity_picture"] == "https://upload.example/ship.jpg"


def test_phone_location_requests_update_when_stale(tmp_path, monkeypatch):
    import time
    from datetime import datetime, timezone

    calls = []
    state = {"updated": time.time() - 3600}

    async def get_state(request):
        ts = datetime.fromtimestamp(state["updated"], timezone.utc).isoformat()
        return web.json_response({"state": "not_home", "last_updated": ts,
                                  "attributes": {"latitude": 60.1, "longitude": 24.5}})

    async def notify(request):
        calls.append(await request.json())
        state["updated"] = time.time()  # the phone answers immediately
        return web.json_response([])

    async def run():
        app = web.Application()
        app.router.add_get("/api/states/{entity}", get_state)
        app.router.add_post("/api/services/notify/{service}", notify)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        monkeypatch.setattr(config, "HA_API", f"http://127.0.0.1:{port}/api")
        monkeypatch.setattr(ha.asyncio, "sleep", _no_sleep)
        async with aiohttp.ClientSession() as session:
            fix = await ha.HAClient(session, "t").phone_location(
                "device_tracker.phone", "notify.mobile_app_phone", 900)
        await runner.cleanup()
        return fix

    fix = asyncio.run(run())
    assert calls == [{"message": "request_location_update"}]
    assert fix[:2] == (60.1, 24.5) and fix[2] < 60


_real_sleep = asyncio.sleep


async def _no_sleep(seconds):
    await _real_sleep(0)
