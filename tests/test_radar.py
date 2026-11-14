"""The rain radar overlay: frames and styles from GetCapabilities, the tile address."""

import asyncio

from ferrywatch import radar
from ferrywatch.overlays import Overlays, parse_instant

LAYER_CAPS = """<WMS_Capabilities version="1.3.0"><Capability><Layer><Title>Radar</Title>
<Layer queryable="1" opaque="0"><Name>radar_finland_cappi_dbzh</Name><Title>dBZ</Title>
<Dimension name="elevation" units="EPSG:5030">0</Dimension>
<Dimension name="time" default="current" units="ISO8601">2026-10-19T16:00:00.000Z/2026-10-25T16:00:00.000Z/PT5M</Dimension>
<Style><Name>radar_dbz_winter_-20_40</Name><Title>winter</Title></Style>
<Style><Name>radar_dbz_summer_8_50_nodata</Name><Title>summer</Title></Style>
</Layer></Layer></Capability></WMS_Capabilities>"""

NAMESPACE_CAPS = """<WMS_Capabilities><Capability><Layer>
<Layer><Name>Radar:radar_finland_cappi_rate</Name>
<Dimension name="time" units="ISO8601">2020-01-01T00:00:00Z</Dimension></Layer>
<Layer><Name>Radar:radar_finland_cappi_dbzh</Name>
<Dimension name="time" units="ISO8601">2026-07-01T10:00:00Z,2026-07-01T10:05:00Z</Dimension>
<Style><Name>radar_dbz_summer_8_50</Name></Style></Layer>
</Layer></Capability></WMS_Capabilities>"""


def test_frames_and_styles_from_the_layers_own_capabilities():
    times, styles = radar.read_capabilities(LAYER_CAPS)
    assert len(times) == radar.FRAMES and times[-1] == parse_instant("2026-10-25T16:00:00Z")
    assert times[1] - times[0] == 300
    assert styles == ["radar_dbz_winter_-20_40", "radar_dbz_summer_8_50_nodata"]


def test_the_right_layer_in_the_namespace_capabilities():
    times, styles = radar.read_capabilities(NAMESPACE_CAPS)
    assert times == [parse_instant("2026-07-01T10:00:00Z"), parse_instant("2026-07-01T10:05:00Z")]
    assert styles == ["radar_dbz_summer_8_50"]
    assert radar.read_capabilities("<WMS_Capabilities/>") == ([], [])


def test_style_by_season_and_what_the_server_lists():
    winter, summer = parse_instant("2026-10-25T12:00:00Z"), parse_instant("2026-07-01T12:00:00Z")
    assert radar.pick_style([], winter) == radar.STYLE_WINTER  # nothing listed: as planned
    assert radar.pick_style([], summer) == radar.STYLE_SUMMER
    assert radar.pick_style(["radar_dbz_winter_-20_40"], summer) == radar.STYLE_WINTER  # summer not listed
    assert radar.pick_style(["something_else"], winter) == ""  # the layer's default


class FakeResponse:
    def __init__(self, status, text):
        self.status, self._text = status, text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def raise_for_status(self):
        if self.status >= 400:
            raise OSError(f"HTTP {self.status}")

    async def text(self):
        return self._text


class FakeSession:
    def __init__(self, answers):
        self.answers, self.asked = answers, []

    def get(self, url, **kw):
        self.asked.append(url)
        return FakeResponse(*self.answers[len(self.asked) - 1])


def test_times_fall_back_to_the_namespace_then_the_clock(tmp_path):
    now = parse_instant("2026-07-01T10:07:00Z")
    o = radar.overlay()
    session = FakeSession([(404, ""), (200, NAMESPACE_CAPS)])
    times = asyncio.run(o.times(session, now))
    assert times == [parse_instant("2026-07-01T10:00:00Z"), parse_instant("2026-07-01T10:05:00Z")]
    assert session.asked == list(radar.CAPS_URLS) and "STYLES=radar_dbz_summer_8_50&" in o.url
    times = asyncio.run(o.times(FakeSession([(500, ""), (500, "")]), now))
    assert len(times) == radar.FRAMES and times[-1] == parse_instant("2026-07-01T09:55:00Z")  # 10 min lag
    assert asyncio.run(o.times(None, now)) == times


def test_registered_as_wms_tiles_kept_six_hours(tmp_path):
    ov = Overlays(tmp_path, None)
    radar.register(ov)
    o = ov.registry["rain-radar"]
    assert o.frame_ms == 500 and o.max_age == 6 * 3600 and ov.cache.max_ages["rain-radar"] == 6 * 3600
    url = o.tile_url(parse_instant("2026-10-25T16:00:00Z"), 8, 145, 74)
    assert url.startswith("https://openwms.fmi.fi/geoserver/Radar/wms?SERVICE=WMS&VERSION=1.3.0&REQUEST=GetMap")
    assert "LAYERS=radar_finland_cappi_dbzh" in url and "CRS=EPSG:3857" in url
    assert "TIME=2026-10-25T16:00:00Z" in url and "BBOX=2661231.58,8296780.80,2817774.61,8453323.83" in url
