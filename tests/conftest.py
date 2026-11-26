"""Suite-wide defaults.

The login drives Chrome through the raw CDP client unless told otherwise; the
tests fake the Playwright surface, so they run on that engine and the CDP
client gets its own tests against the Chromium Playwright installed.
"""
import os

import pytest


@pytest.fixture(autouse=True)
def _playwright_engine(monkeypatch):
    monkeypatch.setenv("INSS_PAGE_ENGINE", "playwright")
