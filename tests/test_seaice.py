"""The sea ice chart overlay: season, days from GetCapabilities, the tile address."""

import asyncio

from ferrywatch import seaice
from ferrywatch.overlays import Overlays, parse_instant

CAPS = """<Capabilities><Contents>
<Layer><ows:Identifier>OTHER/layer</ows:Identifier><Dimension><ows:Identifier>time</ows:Identifier>
<Value>2020-01-01T00:00:00.000Z</Value></Dimension></Layer>
<Layer><ows:Identifier>%s</ows:Identifier><Style><ows:Identifier>cmap:ice</ows:Identifier></Style>
<Dimension><ows:Identifier>elevation</ows:Identifier><Value>0</Value></Dimension>
<Dimension><ows:Identifier>time</ows:Identifier>
<Value>2025-11-01T00:00:00.000Z/2026-01-20T00:00:00.000Z/P1D</Value></Dimension></Layer>
</Contents></Capabilities>""" % seaice.CHART_LAYER


def test_season_is_november_to_may():
    assert seaice.in_season(parse_instant("2025-11-01T00:00:00Z"))
    assert seaice.in_season(parse_instant("2026-05-31T12:00:00Z"))
    assert not seaice.in_season(parse_instant("2026-06-01T00:00:00Z"))
    assert not seaice.in_season(parse_instant("2025-10-25T00:00:00Z"))


def test_days_from_capabilities_take_the_right_layer():
    days = seaice.times_from_capabilities(CAPS)
    assert len(days) == seaice.DAYS
    assert days[-1] == parse_instant("2026-01-20") and days[1] - days[0] == 86400
    assert seaice.times_from_capabilities("<Capabilities/>") == []


def test_no_session_falls_back_to_daily():
    now = parse_instant("2026-01-20T10:00:00Z")
    days = asyncio.run(seaice.list_times(None, now))
    assert len(days) == seaice.DAYS and days[-1] == parse_instant("2026-01-19")


def test_registered_with_style_and_attribution(tmp_path):
    ov = Overlays(tmp_path, None)
    seaice.register(ov)
    o = ov.registry["sea-ice"]
    assert o.opacity == 0.6 and "Copernicus Marine" in o.attribution
    url = o.tile_url(parse_instant("2026-01-19"), 6, 36, 18)
    assert "LAYER=" + seaice.CHART_LAYER in url and "STYLE=cmap:ice" in url
    assert "TILEMATRIX=6&TILEROW=18&TILECOL=36" in url and "TIME=2026-01-19T00:00:00Z" in url
    assert ov.cache.max_ages["sea-ice"] == o.max_age
