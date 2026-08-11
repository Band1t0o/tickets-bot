"""The page and the server it talks to must be the same generation.

This exists because of a real hour lost. Two `uvicorn` processes were left
running from an earlier commit. Static files are read from disk per request, so
both served the *newest* HTML and JS; only the Python was frozen at import time.
The page therefore asked a stale server for `/api/sources` and for scenarios
carrying fields that version had never heard of, got 404 and 400 back, and
rendered an empty trip picker and an empty Prices tab.

An empty picker is what "you have no saved trips" looks like. So the app said
"your data is gone" when the truth was "restart me". Nothing on disk had
changed.

The fix is a single integer both sides carry. These tests keep the two copies
honest, because a constant that only one side ever updates detects nothing.
"""
from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

from src.web.app import API_CONTRACT, app

STATIC = Path(__file__).resolve().parents[1] / "src" / "web" / "static"


def _js_contract() -> int:
    source = (STATIC / "app.js").read_text(encoding="utf-8")
    match = re.search(r"const EXPECTED_CONTRACT\s*=\s*(\d+)", source)
    assert match, "app.js must declare `const EXPECTED_CONTRACT = <n>`"
    return int(match.group(1))


def test_the_page_and_the_server_agree_on_the_contract():
    """Bumping one side without the other would make every page look stale."""
    assert _js_contract() == API_CONTRACT


def test_version_endpoint_reports_the_contract():
    body = TestClient(app).get("/api/version").json()
    assert body["contract"] == API_CONTRACT


def test_version_endpoint_is_cheap_enough_to_ask_first():
    """It runs before anything else on every page load, so it must not touch
    the filesystem or a scenario file - the very things that may be broken."""
    body = TestClient(app).get("/api/version").json()
    assert set(body) == {"contract"}
