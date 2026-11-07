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
const copy = (v) => JSON.parse(JSON.stringify(v));
const A = [60, 24], B = [60, 24.2], C = [60.1, 24.4], T1 = [60.01, 24.1], T2 = [60.05, 24.3], X = [60.03, 24.25];
let stops = [], legs = [];
out.first = P.addStop(stops, legs, A, true);
out.second = P.addStop(stops, legs, B, true);
legs[0].path = [T1, T2]; legs[0].planned = true;
out.third = P.addStop(stops, legs, C, false);
out.points = P.points(stops, legs);
out.legPoints = P.legPoints(stops, legs, 0);
out.saved = P.toSaved(stops, legs);
out.back = P.fromSaved(out.saved.points, out.saved.plan);
out.plain = P.toSaved([A, B], [{ fw: false, path: [] }]);
out.broken = P.fromSaved([A, T1, B], { wp: [0, 5], fw: [1] });
out.mixed = P.fromSaved([A, T1, B, T2, C], { wp: [0, 2, 4], fw: [1, 0] });
out.sections0 = P.sections(stops);
out.kinds0 = out.sections0.map(([a, b]) => P.sectionKind(legs, a, b));

// Pull the fairway leg's second piece out to X: the leg is unpacked, nothing to plan again.
const keep = copy({ stops, legs });
out.bendAt = P.insertBend(stops, legs, 0, 1, X);
out.afterBend = copy({ stops, legs });
out.bentPoints = P.points(stops, legs);
out.sections1 = P.sections(stops);
out.kinds1 = out.sections1.map(([a, b]) => P.sectionKind(legs, a, b));
out.sectionPoints = P.sectionPoints(stops, legs, 0, 4);
out.moveBend = P.moveStop(stops, legs, 2, [60.04, 24.26]);
out.bendSaved = P.toSaved(stops, legs);
out.bendBack = P.fromSaved(out.bendSaved.points, out.bendSaved.plan);

// Moving one of the user's stops next to an automatic fairway leg plans it again.
stops = keep.stops; legs = keep.legs;
out.moveStop = P.moveStop(stops, legs, 1, [60, 24.21]);
legs[0].path = [T1, T2];

// A turn as a stop of its own.
let s2 = copy(stops), l2 = copy(legs);
out.turnStop = P.turnStop(s2, l2, 0, 1);
out.turnStops = s2;

// Straight from the start to the end: everything between goes.
s2 = copy(stops); l2 = copy(legs);
out.cut = P.setSection(s2, l2, 2, 0, false);
out.afterCut = { stops: s2, legs: l2 };
// Back along the fairways between the stops of a section.
s2 = copy(out.afterBend.stops); l2 = copy(out.afterBend.legs);
out.refw = P.setSection(s2, l2, 0, 4, true);
out.afterRefw = { stops: s2, legs: l2 };

// Removing the end when a bend comes before it: the bend becomes the end, a stop.
s2 = [A, [60.02, 24.1, 1], B]; l2 = [{ fw: false, path: [] }, { fw: false, path: [] }];
P.removeStop(s2, l2, 2);
out.endFixed = s2;
// Removing a stop between two fairway legs: one fairway leg, planned again.
s2 = [A, B, C]; l2 = [{ fw: true, path: [T1] }, { fw: true, path: [] }];
out.removeFw = P.removeStop(s2, l2, 1);

// Drag the fairway leg onto another fairway at V: a via point, both halves planned to it.
const V = [60.04, 24.2], W = [60.045, 24.22];
const s3 = [A, B, C], l3 = [{ fw: true, path: [T1, T2] }, { fw: false, path: [] }];
out.via = P.insertVia(s3, l3, 0, V);
out.viaStops = copy(s3);
out.viaSections = P.sections(s3);
out.viaKinds = out.viaSections.map(([a, b]) => P.sectionKind(l3, a, b));
l3[0].path = [T1]; l3[1].path = [T2];
out.viaSaved = P.toSaved(s3, l3);
out.viaBack = P.fromSaved(out.viaSaved.points, out.viaSaved.plan).stops;
out.viaMoved = P.moveStop(s3, l3, 1, W);
out.viaMovedStop = s3[1];
l3[0].path = [T1]; l3[1].path = [T2];
// ...then off the fairways: a bend between the turns either side.
out.viaBend = P.viaToBend(s3, l3, 1, X);
out.viaBendStops = copy(s3);
out.viaBendFw = l3.map((l) => l.fw);
// A bend dragged onto a fairway becomes a via point.
out.toVia = P.toVia(s3, l3, 2, V);
out.toViaStop = s3[2];
out.toViaFw = l3.map((l) => l.fw);
// Removing a via point between two fairway legs: one fairway leg, planned again.
const s4 = [A, V.concat(2), C], l4 = [{ fw: true, path: [] }, { fw: true, path: [] }];
out.viaRemoved = P.removeStop(s4, l4, 1);
out.viaRemovedStops = s4;
// A via point at either end is a numbered stop.
const s5 = [A, V.concat(2)], l5 = [{ fw: true, path: [] }];
P.removeStop(s5, l5, 0);
out.viaEnd = s5;
console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(NODE is None, reason="needs Node")
def test_route_plan():
    out = json.loads(subprocess.run([NODE, "-e", SCRIPT, str(ROUTEPLAN)], capture_output=True, text=True,
                                    check=True).stdout)
    A, B, C, T1, T2, X = [60, 24], [60, 24.2], [60.1, 24.4], [60.01, 24.1], [60.05, 24.3], [60.03, 24.25]
    assert out["first"] == [] and out["second"] == [0] and out["third"] == []
    assert out["points"] == [A, T1, T2, B, C]
    assert out["legPoints"] == [A, T1, T2, B]
    assert out["saved"] == {"points": [A, T1, T2, B, C], "plan": {"wp": [0, 3, 4], "fw": [1, 0]}}
    assert out["back"]["stops"] == [A, B, C]
    assert [(l["fw"], l["path"]) for l in out["back"]["legs"]] == [(True, [T1, T2]), (False, [])]
    assert out["plain"] == {"points": [A, B], "plan": None}
    assert out["broken"]["stops"] == [A, T1, B] and not any(l["fw"] for l in out["broken"]["legs"])
    assert out["mixed"]["stops"] == [A, B, T2, C]
    assert out["sections0"] == [[0, 1], [1, 2]] and out["kinds0"] == ["fw", "line"]

    # The pulled-out bend sits between the two turns; the turns are bends now too.
    assert out["bendAt"] == 2
    assert out["afterBend"]["stops"] == [A, T1 + [1], X + [1], T2 + [1], B, C]
    assert not any(l["fw"] for l in out["afterBend"]["legs"])
    assert out["bentPoints"] == [A, T1, X, T2, B, C]
    assert out["sections1"] == [[0, 4], [4, 5]] and out["kinds1"] == ["own", "line"]
    assert out["sectionPoints"] == [A, T1, X, T2, B]
    assert out["moveBend"] == []
    assert out["bendSaved"]["plan"] == {"wp": [0, 1, 2, 3, 4, 5], "fw": [0] * 5, "bend": [1, 2, 3]}
    assert out["bendBack"]["stops"] == [A, T1 + [1], [60.04, 24.26, 1], T2 + [1], B, C]

    assert out["moveStop"] == [0]
    assert out["turnStop"] == 2 and out["turnStops"] == [A, T1 + [1], T2 + [1], [60, 24.21], C]
    assert out["cut"] == []
    assert out["afterCut"]["stops"] == [A, C] and out["afterCut"]["legs"] == [{"fw": False, "path": []}]
    assert out["refw"] == [0]
    assert out["afterRefw"]["stops"] == [A, B, C] and [l["fw"] for l in out["afterRefw"]["legs"]] == [True, False]
    assert out["endFixed"] == [A, [60.02, 24.1]]
    assert out["removeFw"] == [0]

    V, W = [60.04, 24.2], [60.045, 24.22]
    assert out["via"] == [0, 1]
    assert out["viaStops"] == [A, V + [2], B, C]
    assert out["viaSections"] == [[0, 2], [2, 3]] and out["viaKinds"] == ["fw", "line"]  # no number on it
    assert out["viaSaved"] == {"points": [A, T1, V, T2, B, C], "plan": {"wp": [0, 2, 4, 5], "fw": [1, 1, 0], "via": [1]}}
    assert out["viaBack"] == [A, V + [2], B, C]
    assert out["viaMoved"] == [0, 1] and out["viaMovedStop"] == W + [2]
    assert out["viaBend"] == 2
    assert out["viaBendStops"] == [A, T1 + [1], X + [1], T2 + [1], B, C]
    assert out["viaBendFw"] == [False] * 5
    assert out["toVia"] == [1, 2] and out["toViaStop"] == V + [2]
    assert out["toViaFw"] == [False, True, True, False, False]
    assert out["viaRemoved"] == [0] and out["viaRemovedStops"] == [A, C]
    assert out["viaEnd"] == [V]
