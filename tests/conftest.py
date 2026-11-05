from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def clean_urep_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(os.environ):
        if key.startswith("UREP_"):
            monkeypatch.delenv(key, raising=False)
