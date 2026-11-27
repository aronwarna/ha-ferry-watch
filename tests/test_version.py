"""The version is written in three places; an update only reaches Home Assistant when they agree."""
import re
from pathlib import Path

import ferrywatch

ROOT = Path(__file__).resolve().parents[1] / "ferry_watch"


def test_versions_agree():
    manifest = re.search(r'^version: "([^"]+)"', (ROOT / "config.yaml").read_text(), re.M).group(1)
    changelog = re.search(r"^## (\S+)", (ROOT / "CHANGELOG.md").read_text(), re.M).group(1)
    assert manifest == ferrywatch.__version__ == changelog
