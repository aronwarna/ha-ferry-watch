import csv
import io

from ferrywatch import stats
from ferrywatch.store import Store


def passage(store, mmsi, ts, route="inner", direction="west", notified=False, note=None):
    pid = store.add_passage({"mmsi": mmsi, "ts": ts, "route": route, "direction": direction,
                             "zone_id": None, "lat": 60.0, "lon": 24.4, "sog": 17.25})
    store.set_passage_result(pid, notified, note)
    return pid


def test_payload_keeps_removed_ships(tmp_path):
    store = Store(tmp_path / "t.db")
    store.add_ship(1, "Kept", "#111111")
    store.add_ship(2, "Removed", "#222222")
    store.add_ship(3, "Never passed", "#333333")
    passage(store, 1, 2000, direction="east")
    passage(store, 2, 1000)
    store.remove_ship(2)
    store.remove_ship(3)

    data = stats.payload(store)
    by_mmsi = {s["mmsi"]: s for s in data["ships"]}
    assert by_mmsi[1]["current"] and by_mmsi[1]["name"] == "Kept"
    assert by_mmsi[2] == {"mmsi": 2, "name": "Removed", "color": "#222222", "current": False}
    assert 3 not in by_mmsi  # removed and no passages: nothing to show
    assert data["passages"] == [[1000, 2, "inner", "west", None, None, None],
                                [2000, 1, "inner", "east", None, None, None]]


def test_change_mmsi_moves_passages(tmp_path):
    store = Store(tmp_path / "t.db")
    store.add_ship(1, "Ship", "#111111")
    passage(store, 1, 1000)
    store.change_mmsi(1, 9)
    assert stats.payload(store)["passages"] == [[1000, 9, "inner", "west", None, None, None]]


def test_csv(tmp_path):
    store = Store(tmp_path / "t.db")
    store.add_ship(1, "Silja Serenade", "#111111")
    passage(store, 1, 1_790_000_000, notified=True, note="sent")
    pid = passage(store, 1, 1_790_050_000, route="outer", direction="east", note="outer route")
    store.set_passage_wind(pid, 8.36, 11.0, 225.0)
    store.set_passage_conditions(pid, water_c=1.24, vis_km=12.0, ice="ice", ice_cm=31.0, ice_conc=90.0, bogus=1)
    rows = list(csv.reader(io.StringIO(stats.to_csv(store))))
    assert rows[0] == ["time", "ship", "mmsi", "route", "direction", "speed_kn", "alert", "wind_ms", "wind_dir",
                       "gust_ms", "water_c", "visibility_km", "ice", "ice_cm", "ice_concentration"]
    assert rows[1][1:] == ["Silja Serenade", "1", "outer", "towards Helsinki", "17.2", "outer route", "8.4", "225",
                           "11.0", "1.2", "12.0", "ice", "31", "90"]
    assert rows[2][1:] == ["Silja Serenade", "1", "inner", "towards Stockholm", "17.2", "sent", "", "",
                           "", "", "", "", "", ""]
    assert stats.payload(store)["passages"][1][4:] == [8.4, 225.0, "ice"]
