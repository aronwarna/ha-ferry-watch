import asyncio

from ferrywatch import ashore, simmaps


def test_parse_ashore():
    rows = ashore.parse_ashore(asyncio.run(simmaps.fake_ashore_fetch("")))
    kinds = {r[4] for r in rows}
    assert {"Lean-to shelter", "Campfire site", "Wilderness hut", "Sauna", "Toilet"} <= kinds
    lavu = next(r for r in rows if r[4] == "Lean-to shelter")
    assert lavu[3] == "Rönnskärin laavu" and lavu[5] == ["fire", "wood"]
    assert all(r[1] < 60.2 for r in rows)  # the inland one is left out
    assert "lean_to" in ashore.ashore_query()
