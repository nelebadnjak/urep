from __future__ import annotations

import threading
from pathlib import Path

import pytest

from urep.watcher import FileWatcher


@pytest.mark.unit
def test_watcher_detects_write(tmp_path: Path) -> None:
    path = tmp_path / "app.yaml"
    path.write_text("one\n")

    seen: list[Path] = []
    ready = threading.Event()

    def on_batch(paths: list[Path]) -> None:
        seen.extend(paths)
        ready.set()

    watcher = FileWatcher([path], debounce_ms=100, on_batch=on_batch)
    watcher.start()
    try:
        assert watcher.wait_ready(5)
        path.write_text("two\n")
        assert ready.wait(5)
        assert path in seen
    finally:
        watcher.stop()


@pytest.mark.unit
def test_watcher_detects_delete(tmp_path: Path) -> None:
    path = tmp_path / "app.yaml"
    path.write_text("one\n")

    seen: list[Path] = []
    ready = threading.Event()

    def on_batch(paths: list[Path]) -> None:
        seen.extend(paths)
        ready.set()

    watcher = FileWatcher([path], debounce_ms=100, on_batch=on_batch)
    watcher.start()
    try:
        assert watcher.wait_ready(5)
        path.unlink()
        assert ready.wait(5)
        assert path in seen
    finally:
        watcher.stop()
