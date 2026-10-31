"""The route tool's maths (static/navmath.js), run under Node."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

NAVMATH = Path(__file__).resolve().parents[1] / "ferry_watch" / "app" / "ferrywatch" / "static" / "navmath.js"
NODE = shutil.which("node")

SCRIPT = """
const M = require(process.argv[1]);
const HEL = [60.1699, 24.9384], TLL = [59.4370, 24.7536];
const route = [[59.98, 24.42], [59.95, 24.38], [59.93, 24.30]];
console.log(JSON.stringify({
  north: M.distanceNm([60, 24], [61, 24]),
  east: M.distanceNm([60, 24], [60, 25]),
  eastBearing: M.rhumbBearing([60, 24], [60, 25]),
  helTll: M.distanceNm(HEL, TLL),
  helTllBearing: M.rhumbBearing(HEL, TLL),
  back: M.rhumbBearing(TLL, HEL),
  dateline: M.rhumbBearing([0, 179.5], [0, -179.5]),
  legs: M.legs(route),
  total: M.totalNm(route),
  single: M.totalNm([[60, 24]]),
  eta: M.etaSeconds(12.5, 25),
  noSpeed: M.etaSeconds(12.5, 0),
  texts: [M.nm(3.04), M.degrees(5.4), M.degrees(359.7), M.duration(29.6 * 60), M.duration(3900),
          M.position(59.98333, 24.425), M.position(-0.5, -0.99999999)],
}));
"""


@pytest.mark.skipif(NODE is None, reason="needs Node")
def test_route_maths():
    out = json.loads(subprocess.run([NODE, "-e", SCRIPT, str(NAVMATH)], capture_output=True, text=True,
                                    check=True).stdout)
    assert out["north"] == pytest.approx(60.04, abs=0.01)  # a degree of latitude
    assert out["east"] == pytest.approx(30.0, abs=0.05)  # a degree of longitude at 60°N
    assert out["eastBearing"] == pytest.approx(90, abs=1e-9)
    assert out["helTll"] == pytest.approx(44.35, abs=0.05)  # Helsinki to Tallinn, about 82 km
    assert out["helTllBearing"] == pytest.approx(187.3, abs=0.5)
    assert out["back"] == pytest.approx((out["helTllBearing"] + 180) % 360, abs=1e-6)  # rhumb lines reverse exactly
    assert out["dateline"] == pytest.approx(90, abs=1e-6)  # the short way across 180°
    legs = out["legs"]
    assert len(legs) == 2 and legs[0]["nm"] == pytest.approx(2.17, abs=0.01)
    assert 212 < legs[0]["bearing"] < 216 and 240 < legs[1]["bearing"] < 250
    assert out["total"] == pytest.approx(legs[0]["nm"] + legs[1]["nm"])
    assert out["single"] == 0
    assert out["eta"] == 1800 and out["noSpeed"] is None
    assert out["texts"] == ["3.0 NM", "005°", "000°", "30 min", "1 h 05 min", "59°59.000′N 024°25.500′E",
                            "00°30.000′S 001°00.000′W"]
