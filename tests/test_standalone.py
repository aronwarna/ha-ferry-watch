"""Running outside Home Assistant: options from the environment, the panel's password,
and alerts through ntfy when Home Assistant doesn't answer."""
import asyncio
import base64
import time
from types import SimpleNamespace

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import ferrywatch.config as config
import ferrywatch.notify as notify
import ferrywatch.web as webmod
from ferrywatch.config import Options, env_options
from ferrywatch.simulate import FakeHA
from ferrywatch.store import Store
from ferrywatch.tracker import Tracker

MMSI = 230184000


def test_options_from_the_environment():
    opts = env_options(Options(), {"FW_NOTIFY_SERVICE": "notify.me", "FW_RETENTION_DAYS": "90",
                                   "FW_REQUIRE_PHONE_AREA": "false", "FW_PHONE_MAX_AGE_MINUTES": "soon",
                                   "FW_LOG_LEVEL": ""})
    assert opts.notify_service == "notify.me" and opts.retention_days == 90
    assert opts.require_phone_area is False
    assert opts.phone_max_age_minutes == 15 and opts.log_level == "info"  # bad or empty: left alone


def _app():
    async def hello(request):
        return web.Response(text="hi")
    app = web.Application(middlewares=[webmod.ingress_only])
    for path in ("/", "/api/health", "/api/notify/picture/x.png"):
        app.router.add_get(path, hello)
    return app


def _get(monkeypatch, standalone, password, path="/", headers=None, cookies=None):
    monkeypatch.setattr(webmod, "DEV", False)
    monkeypatch.setattr(config, "STANDALONE", standalone)
    monkeypatch.setattr(config, "PASSWORD", password)

    async def run():
        async with TestClient(TestServer(_app()), cookies=cookies) as client:
            resp = await client.get(path, headers=headers or {})
            return resp.status, resp.cookies.get(webmod.AUTH_COOKIE)
    return asyncio.run(run())


def test_standalone_panel_is_open_without_a_password(monkeypatch):
    assert _get(monkeypatch, True, None)[0] == 200


def test_standalone_password(monkeypatch):
    assert _get(monkeypatch, True, "sesame")[0] == 401
    wrong = {"Authorization": "Basic " + base64.b64encode(b"ben:nope").decode()}
    assert _get(monkeypatch, True, "sesame", headers=wrong)[0] == 401
    right = {"Authorization": "Basic " + base64.b64encode(b"ben:sesame").decode()}
    status, cookie = _get(monkeypatch, True, "sesame", headers=right)
    assert status == 200 and cookie is not None
    # The cookie alone is enough afterwards (the live connection sends no login).
    assert _get(monkeypatch, True, "sesame", cookies={webmod.AUTH_COOKIE: cookie.value})[0] == 200
    assert _get(monkeypatch, True, "sesame", cookies={webmod.AUTH_COOKIE: "made-up"})[0] == 401
    # The notification picture and the health check need no login.
    assert _get(monkeypatch, True, "sesame", path="/api/notify/picture/x.png")[0] == 200
    assert _get(monkeypatch, True, "sesame", path="/api/health")[0] == 200


class _Posts:
    """Stands in for the HTTP session: keeps what was posted to ntfy."""
    def __init__(self):
        self.sent = []

    def post(self, url, json=None, timeout=None):
        self.sent.append((url, json))
        resp = SimpleNamespace(raise_for_status=lambda: None)

        class Ctx:
            async def __aenter__(self_inner):
                return resp

            async def __aexit__(self_inner, *a):
                return False
        return Ctx()


class DownHA(FakeHA):
    async def reachable(self):
        return False


def _fw(tmp_path, monkeypatch, ha, ntfy="https://ntfy.example/ferries-abc"):
    monkeypatch.setattr(config, "MEDIA_DIR", tmp_path / "media")
    monkeypatch.setattr(config, "NTFY_URL", ntfy)

    async def no_picture(self, passage, phone):
        return None
    monkeypatch.setattr(notify.Notifier, "picture", no_picture)
    store = Store(tmp_path / "t.db")
    store.add_ship(MMSI, "Silja Serenade", "#2563eb")
    store.add_zone("Me", "phone", "polygon", [[60.2, 24.3], [60.2, 24.6], [60.0, 24.6], [60.0, 24.3]])
    tracker = Tracker(store)
    tracker.load()
    posts = _Posts()
    fw = SimpleNamespace(store=store, tracker=tracker, ha=ha, options=Options(), session=posts,
                         photos=SimpleNamespace(remote_url=lambda m: None))
    return fw, notify.Notifier(fw), posts


def _passage():
    return {"id": 1, "mmsi": MMSI, "ts": int(time.time()), "route": "inner", "direction": "west",
            "lat": 60.0, "lon": 24.4, "sog": 17.0}


def test_alert_goes_through_ntfy_when_home_assistant_is_down(tmp_path, monkeypatch):
    ha = DownHA()
    fw, n, posts = _fw(tmp_path, monkeypatch, ha)
    sent, note = asyncio.run(n._alert(_passage()))
    assert sent and "ntfy" in note
    assert not ha.calls
    (url, body), = posts.sent
    assert url == "https://ntfy.example" and body["topic"] == "ferries-abc"
    assert body["title"] == "Silja Serenade: inner route" and body["priority"] == 4


def test_alert_without_home_assistant_at_all(tmp_path, monkeypatch):
    fw, n, posts = _fw(tmp_path, monkeypatch, None)
    assert asyncio.run(n._alert(_passage()))[0] and len(posts.sent) == 1
    (tmp_path / "b").mkdir()
    fw, n, posts = _fw(tmp_path / "b", monkeypatch, None, ntfy=None)
    assert asyncio.run(n._alert(_passage())) == (False, "Home Assistant not connected")


def test_home_assistant_is_used_when_it_answers(tmp_path, monkeypatch):
    ha = FakeHA((60.115, 24.45))
    fw, n, posts = _fw(tmp_path, monkeypatch, ha)
    assert asyncio.run(n._alert(_passage())) == (True, "sent")
    assert len(ha.calls) == 1 and not posts.sent


def test_down_home_assistant_and_no_ntfy_sends_nothing(tmp_path, monkeypatch):
    fw, n, posts = _fw(tmp_path, monkeypatch, DownHA(), ntfy=None)
    assert asyncio.run(n._alert(_passage())) == (False, "Home Assistant not answering")
