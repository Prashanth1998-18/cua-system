"""
Shared pytest fixtures.

Tests must be deterministic regardless of what's in a developer's local
.env file (config.py calls load_dotenv() at import time), so every test
runs with the CUA_* environment variables cleared unless a test explicitly
sets one via monkeypatch.
"""

from __future__ import annotations

import pytest

_CUA_ENV_PREFIXES = ("CUA_",)


@pytest.fixture(autouse=True)
def _clear_cua_env(monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    for key in list(os.environ):
        if key.startswith(_CUA_ENV_PREFIXES):
            monkeypatch.delenv(key, raising=False)
