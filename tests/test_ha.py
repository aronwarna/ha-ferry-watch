import asyncio
from types import SimpleNamespace

import aiohttp
from aiohttp import web

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
        monkeypatch.setattr(ha, "HA_API", f"http://127.0.0.1:{port}/api")

        store = Store(tmp_path / "t.db")
        store.add_ship(230184000, "Silja Serenade", "#000")
        tracker = Tracker(store)
        tracker.load()
        tracker.on_location(230184000, {"time": 1000, "lat": 60.1, "lon": 24.9, "sog": 15.0, "cog": 250.0})
        photos = SimpleNamespace(remote_url=lambda mmsi: "https://upload.example/ship.jpg")
        fw = SimpleNamespace(store=store, tracker=tracker, photos=photos)
        async with aiohttp.ClientSession() as session:
            await ha.HAPublisher(session, "token123", 60, fw).publish(230184000)
        await runner.cleanup()

    asyncio.run(run())
    entity, auth, body = received[0]
    assert entity == "device_tracker.ferry_watch_silja_serenade"
    assert auth == "Bearer token123"
    assert body["state"] == "under_way"
    assert body["attributes"]["latitude"] == 60.1
    assert body["attributes"]["entity_picture"] == "https://upload.example/ship.jpg"
