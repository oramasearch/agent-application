# SPDX-License-Identifier: MIT

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def lake_url(monkeypatch):
    """The service sends a turn's credential only to its own LAKE_URL (#30), so every test runs
    with one set; tests that need another value set it themselves."""
    monkeypatch.setenv("LAKE_URL", "http://lake:8080")
