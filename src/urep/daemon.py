from __future__ import annotations

import logging
import os
import signal
import stat
import threading
import time
from pathlib import Path

from .config import Config, make_node_id
from .gitrepo import Change, FileState, GitRepo, blob_hash, message_id
from .hlc import Clock
from .paths import ensure_parent, mirror_rel
from .state import EventLog, RuntimeState
from .watcher import FileWatcher

log = logging.getLogger("urep.daemon")


class Daemon:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.node_id = make_node_id(config.prefix)
        self.clock = Clock(self.node_id)
        self.repo = GitRepo(config.repo_dir)
        self.repo.ensure()
        self.state = self.repo.derive_state()
        for item in self.state.values():
            self.clock.observe(item.version)
        self.runtime = RuntimeState(config.state_dir / "state.json")
        self.events = EventLog(config.state_dir / "events.jsonl")
        self.watcher: FileWatcher | None = None

    def run(self) -> None:
        logging.basicConfig(
            level=getattr(logging, self.config.log_level.upper(), logging.INFO),
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )
        log.info(
            "starting node=%s role=%s tags=%s paths=%s",
            self.node_id,
            self.config.role,
            ",".join(self.config.tags) or "-",
            ",".join(str(path) for path in self.config.paths) or "-",
        )
        log.info(
            "repo=%s commits=%d state=%d",
            self.config.repo_dir,
            self.repo.commit_count(),
            len(self.state),
        )

        if self.config.role == "peer":
            for path in self.config.paths:
                ensure_parent(path, self.config.dir_mode)
            if self.config.paths:
                self.watcher = FileWatcher(
                    self.config.paths,
                    self.config.debounce_ms,
                    self._handle_batch,
                    self.config.dir_mode,
                )
                self.watcher.start()
                if not self.watcher.wait_ready(5):
                    log.warning("watcher did not become ready in time")

        stop = threading.Event()

        def _stop(signum: int, frame: object) -> None:
            stop.set()

        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)

        while not stop.wait(1.0):
            pass

        if self.watcher is not None:
            self.watcher.stop()
        log.info("stopped node=%s", self.node_id)

    def _hooks(self) -> tuple[list[list[str]], list[list[str]]]:
        if not self.config.hooks_enabled:
            return [], []
        pre = [["sh", "-c", command] for command in self.config.pre_hooks]
        post = [["sh", "-c", command] for command in self.config.post_hooks]
        return pre, post

    def _handle_batch(self, paths: list[Path]) -> None:
        for path in paths:
            try:
                self._handle_one(path)
            except Exception:
                log.exception("failed to process %s", path)
                self.events.write(
                    level="error",
                    node=self.node_id,
                    action="watch",
                    path=str(path),
                    result="error",
                )

    def _handle_one(self, path: Path) -> None:
        rel = mirror_rel(path)
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            current = self.state.get(rel)
            if current is not None and not current.deleted:
                self._commit_delete(path, rel, current)
            return

        if stat.S_ISLNK(info.st_mode):
            self.events.write(
                level="warning",
                node=self.node_id,
                action="skip",
                path=str(path),
                result="symlink",
            )
            return
        if not stat.S_ISREG(info.st_mode):
            self.events.write(
                level="warning",
                node=self.node_id,
                action="skip",
                path=str(path),
                result="not_a_file",
            )
            return

        data = path.read_bytes()
        if len(data) > self.config.max_file_size:
            self.events.write(
                level="warning",
                node=self.node_id,
                action="skip",
                path=str(path),
                result="too_large",
            )
            return

        blob = blob_hash(data)
        mode = info.st_mode & 0o7777
        current = self.state.get(rel)

        if (
            current is not None
            and not current.deleted
            and current.blob == blob
            and current.mode == mode
        ):
            return

        op = "A" if current is None or current.deleted else "M"
        force = current is not None and not current.deleted and current.blob == blob
        self.repo.write_mirror(rel, data, mode)
        version = self.clock.tick()
        pre, post = self._hooks()
        change = Change(rel, op, blob, mode, version, pre, post)
        if self._commit(path, rel, change, force=force):
            log.info("committed %s (%s %s)", path, op, version)

    def _commit_delete(self, path: Path, rel: str, current: FileState) -> None:
        self.repo.remove_mirror(rel)
        version = self.clock.tick()
        pre, post = self._hooks()
        change = Change(rel, "D", current.blob, current.mode, version, pre, post)
        if self._commit(path, rel, change, force=False):
            log.info("committed delete %s (%s)", path, version)

    def _commit(self, path: Path, rel: str, change: Change, *, force: bool) -> bool:
        msg = message_id("local", rel, change.version, change.blob)
        sha = self.repo.commit(
            node=self.node_id,
            msg_id=msg,
            changes=[change],
            timestamp_ms=int(time.time() * 1000),
            force=force,
        )
        if sha is None:
            return False
        self.state[rel] = FileState(
            path=rel,
            blob=change.blob,
            version=change.version,
            mode=change.mode,
            deleted=change.op == "D",
            pre_hooks=change.pre_hooks,
            post_hooks=change.post_hooks,
            commit=sha,
            ts=int(time.time()),
        )
        self.events.write(
            level="info",
            node=self.node_id,
            action="delete" if change.op == "D" else "update",
            path=str(path),
            peer=None,
            result="ok",
            version=str(change.version),
            commit=sha,
        )
        return True
