from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path

from inotify_simple import INotify, flags

log = logging.getLogger("urep.watcher")


class FileWatcher:
    """Watches parent directories of individual files and debounces changes."""

    def __init__(
        self,
        paths: list[Path],
        debounce_ms: int,
        on_batch: Callable[[list[Path]], None],
        dir_mode: int = 0o755,
    ) -> None:
        self.paths = paths
        self.debounce = debounce_ms / 1000
        self.on_batch = on_batch
        self.dir_mode = dir_mode

        self._inotify = INotify()
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None

        self._paths_by_dir: dict[Path, set[str]] = {}
        for path in paths:
            self._paths_by_dir.setdefault(path.parent, set()).add(path.name)

        self._watch_dirs: dict[Path, int] = {}
        self._wd_dirs: dict[int, Path] = {}
        self._pending: set[Path] = set()
        self._deadline: float | None = None

        self._mask = (
            flags.MODIFY
            | flags.CLOSE_WRITE
            | flags.MOVED_TO
            | flags.MOVED_FROM
            | flags.CREATE
            | flags.DELETE
            | flags.ATTRIB
        )

    def wait_ready(self, timeout: float = 5.0) -> bool:
        return self._ready.wait(timeout)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="urep-watcher", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)
        else:
            self._inotify.close()

    def _ensure_dir(self, directory: Path) -> None:
        if directory in self._watch_dirs:
            return
        if not directory.exists():
            try:
                directory.mkdir(parents=True, exist_ok=True, mode=self.dir_mode)
            except OSError as exc:
                log.warning("cannot create watched directory %s: %s", directory, exc)
                return
        try:
            wd = self._inotify.add_watch(str(directory), self._mask)
        except OSError as exc:
            log.warning("cannot watch %s: %s", directory, exc)
            return
        self._watch_dirs[directory] = wd
        self._wd_dirs[wd] = directory

    def _run(self) -> None:
        try:
            for directory in self._paths_by_dir:
                self._ensure_dir(directory)
            self._ready.set()

            last_check = 0.0
            while not self._stop.is_set():
                try:
                    events = self._inotify.read(timeout=200)
                except (TimeoutError, OSError):
                    events = []

                now = time.monotonic()
                for event in events:
                    if event.mask & flags.IGNORED:
                        ignored_dir = self._wd_dirs.get(event.wd)
                        if ignored_dir is not None:
                            self._wd_dirs.pop(event.wd, None)
                            self._watch_dirs.pop(ignored_dir, None)
                        continue

                    event_dir = self._wd_dirs.get(event.wd)
                    if event_dir is None:
                        continue

                    if event.mask & flags.DELETE_SELF:
                        self._watch_dirs.pop(event_dir, None)
                        self._wd_dirs.pop(event.wd, None)
                        continue

                    name = event.name or ""
                    if name and name in self._paths_by_dir.get(event_dir, set()):
                        self._pending.add(event_dir / name)
                        if self._deadline is None:
                            self._deadline = now + self.debounce

                if now - last_check >= 1.0:
                    for directory in self._paths_by_dir:
                        self._ensure_dir(directory)
                    last_check = now

                if self._deadline is not None and now >= self._deadline:
                    batch = sorted(self._pending)
                    self._pending.clear()
                    self._deadline = None
                    if batch:
                        try:
                            self.on_batch(batch)
                        except Exception:
                            log.exception("watcher callback failed")
        finally:
            self._inotify.close()
