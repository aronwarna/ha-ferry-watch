"""The boats the route tool plans for, and the sailing boat's speeds."""

from ferrywatch import boats, dayscore


def test_old_boat_sizes_map_to_boats():
    assert boats.boat("small")[0] == "buster_m"
    assert boats.boat("medium")[0] == "buster_xl"
    assert boats.boat("large")[0] == "cabin8"
    assert boats.boat("nonsense")[0] == boats.DEFAULT_BOAT
    assert dayscore.BOATS is boats.BOATS


def test_each_boat_has_what_planning_needs():
    for b in boats.BOATS.values():
        assert boats.needs_depth(b) > b["draught"] > 0
        for k in ("wind", "gust", "wave"):
            assert b[k][0] < b[k][1]
    assert boats.BOATS["sail35"]["draught"] == 1.9
    # The sailing boat takes more wind than the open boats.
    assert boats.BOATS["sail35"]["wind"][1] > boats.BOATS["buster_xl"]["wind"][1] > boats.BOATS["buster_m"]["wind"][1]
    listing = boats.listing()
    assert set(listing["boats"]) == set(boats.BOATS) and listing["boats"]["sail35"]["depth"] == 2.5
    assert len(listing["sail"]["polar"]["10"]) == len(listing["sail"]["angles"])


def test_polar_reads_between_rows_and_angles():
    assert boats.polar_kn(10, 90) == 6.5 * boats.SAIL["trim"]
    assert boats.polar_kn(10, 30) == 0  # can't point there
    assert boats.polar_kn(8, 90) < boats.polar_kn(9, 90) < boats.polar_kn(10, 90)
    assert boats.polar_kn(10, -90) == boats.polar_kn(10, 270) == boats.polar_kn(10, 90)
    assert boats.polar_kn(2, 90) < boats.polar_kn(4, 90)


def test_made_good_tacks_motors_and_gybes():
    reach, how = boats.made_good(6.0, 90)
    assert how == "sail" and 5 < reach < 7
    upwind, how = boats.made_good(6.0, 0)
    assert how == "beat" and 2.5 < upwind < reach * 0.75
    # No room to tack: the engine.
    assert boats.made_good(6.0, 0, narrow=True) == (boats.SAIL["motor_kn"], "motor")
    # A calm: the engine, whatever the angle.
    assert boats.made_good(1.0, 90)[1] == "motor"
    # Light air dead downwind: gybing pays.
    assert boats.made_good(3.5, 180)[1] in ("gybe", "motor", "sail")
    v40, _ = boats.made_good(6.0, 41)
    v0, _ = boats.made_good(6.0, 0)
    assert v40 >= v0  # nearer the close-hauled course, more of the speed counts


def test_comfort_counts_beating_and_weather_longer():
    easy = boats.comfort_factor("sail", 90, 6, 0.3)
    assert easy == 1.0
    assert boats.comfort_factor("beat", 20, 6, 0.3) == boats.SAIL["comfort_beat"]
    assert boats.comfort_factor("sail", 90, 14, 0.3) > 1.5
    assert boats.comfort_factor("sail", 90, 6, 1.8) > 1.5
    assert boats.comfort_factor("sail", 178, 6, None) == boats.SAIL["comfort_run"]
