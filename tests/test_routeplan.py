"""The route tool's stops-and-legs plan (static/routeplan.js), run under Node."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROUTEPLAN = Path(__file__).resolve().parents[1] / "ferry_watch" / "app" / "ferrywatch" / "static" / "routeplan.js"
NODE = shutil.which("node")

SCRIPT = """
const P = require(process.argv[1]);
const out = {};
const A = [60, 24], B = [60, 24.2], C = [60.1, 24.4], T1 = [60.01, 24.1], T2 = [60.05, 24.3];
let stops = [], legs = [];
out.first = P.addStop(stops, legs, A, true);
out.second = P.addStop(stops, legs, B, true);
legs[0].path = [T1]; legs[0].planned = true;
out.third = P.addStop(stops, legs, C, false);
out.points = P.points(stops, legs);
out.legPoints = P.legPoints(stops, legs, 0);
out.saved = P.toSaved(stops, legs);
out.back = P.fromSaved(out.saved.points, out.saved.plan);
out.plain = P.toSaved([A, B], [{ fw: false, path: [] }]);
out.broken = P.fromSaved([A, T1, B], { wp: [0, 5], fw: [1] });
// A saved fairway leg next to a straight one with points of its own (an older route).
out.mixed = P.fromSaved([A, T1, B, T2, C], { wp: [0, 2, 4], fw: [1, 0] });
// Bend the fairway leg through a new stop: both halves are planned again.
out.insert = P.insertStop(stops, legs, 0, [60.02, 24.15]);
out.afterInsert = { stops: JSON.parse(JSON.stringify(stops)), legs: JSON.parse(JSON.stringify(legs)) };
out.move = P.moveStop(stops, legs, 2, [60.0, 24.25]);
out.removeMiddleMixed = P.removeStop(stops, legs, 2);  // fairway + straight -> straight
out.afterRemove = { stops: JSON.parse(JSON.stringify(stops)), legs: JSON.parse(JSON.stringify(legs)) };
out.removeMiddleFw = P.removeStop(stops, legs, 1);  // fairway + fairway -> fairway
out.mode = P.setLegMode(legs, 0, false);
legs[0] = { fw: true, path: [T1, T2] };
out.unpack = P.unpackLeg(stops, legs, 0);
out.unpacked = { stops, legs };
console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(NODE is None, reason="needs Node")
def test_route_plan():
    out = json.loads(subprocess.run([NODE, "-e", SCRIPT, str(ROUTEPLAN)], capture_output=True, text=True,
                                    check=True).stdout)
    A, B, C, T1, T2 = [60, 24], [60, 24.2], [60.1, 24.4], [60.01, 24.1], [60.05, 24.3]
    assert out["first"] == [] and out["second"] == [0] and out["third"] == []
    assert out["points"] == [A, T1, B, C]
    assert out["legPoints"] == [A, T1, B]
    assert out["saved"] == {"points": [A, T1, B, C], "plan": {"wp": [0, 2, 3], "fw": [1, 0]}}
    assert out["back"]["stops"] == [A, B, C]
    assert [(l["fw"], l["path"]) for l in out["back"]["legs"]] == [(True, [T1]), (False, [])]
    assert out["plain"] == {"points": [A, B], "plan": None}
    # A plan that doesn't fit the points: every point a stop, straight legs.
    assert out["broken"]["stops"] == [A, T1, B] and not any(l["fw"] for l in out["broken"]["legs"])
    assert out["mixed"]["stops"] == [A, B, T2, C]
    assert [l["fw"] for l in out["mixed"]["legs"]] == [True, False, False]
    assert out["insert"] == [0, 1]
    assert out["afterInsert"]["stops"] == [A, [60.02, 24.15], B, C]
    assert [l["fw"] for l in out["afterInsert"]["legs"]] == [True, True, False]
    assert out["move"] == [1]  # leg 2-3 is straight: only the fairway leg before is planned again
    assert out["removeMiddleMixed"] == []
    assert out["afterRemove"]["stops"] == [A, [60.02, 24.15], C]
    assert [l["fw"] for l in out["afterRemove"]["legs"]] == [True, False]
    assert out["removeMiddleFw"] == []  # the leg after was straight, so the merged one is too
    assert out["mode"] == []
    assert out["unpack"] == []
    assert out["unpacked"]["stops"] == [A, T1, T2, C]
    assert [l["fw"] for l in out["unpacked"]["legs"]] == [False, False, False]
