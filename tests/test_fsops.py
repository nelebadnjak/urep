from __future__ import annotations

import stat
from pathlib import Path

from urep.fsops import atomic_write


def test_atomic_write_creates_file_with_mode(tmp_path: Path) -> None:
    target = tmp_path / "app.yaml"
    assert atomic_write(target, b"one\n", 0o640) is True
    assert target.read_bytes() == b"one\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o640


def test_atomic_write_replaces_existing(tmp_path: Path) -> None:
    target = tmp_path / "app.yaml"
    target.write_bytes(b"old\n")
    assert atomic_write(target, b"new\n", 0o600) is True
    assert target.read_bytes() == b"new\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
