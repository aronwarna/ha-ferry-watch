import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ferry_watch" / "app"))

import pytest


@pytest.fixture(autouse=True)
def _patient_overlay_list(monkeypatch):
    """The overlay list answers without a source slower than LIST_WAIT; the tests' fake
    sources take their time on a busy machine, and expect their times in the list."""
    from ferrywatch import overlays
    monkeypatch.setattr(overlays, "LIST_WAIT", 60.0)
