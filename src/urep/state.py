from __future__ import annotations

import json
import os
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


class RuntimeState:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> dict[str, Any]:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return value if isinstance(value, dict) else {}

    def save(self, data: dict[str, Any]) -> None:
        _atomic_write(
            self.path,
            (json.dumps(data, indent=2, sort_keys=True) + "\n").encode(),
        )

    def paused(self, direction: str | None = None) -> bool:
        data = self.load()
        until = data.get("paused_until")
        if isinstance(until, (int, float)) and time.time() > until:
            return False
        if data.get("paused"):
            return True
        if direction == "in":
            return bool(data.get("paused_in"))
        if direction == "out":
            return bool(data.get("paused_out"))
        return bool(data.get("paused_in") or data.get("paused_out"))

    def pause(
        self,
        inbound: bool = True,
        outbound: bool = True,
        until: float | None = None,
    ) -> None:
        data = self.load()
        data["paused"] = inbound and outbound
        data["paused_in"] = inbound
        data["paused_out"] = outbound
        data["paused_until"] = until
        self.save(data)

    def resume(self) -> None:
        data = self.load()
        data["paused"] = False
        data["paused_in"] = False
        data["paused_out"] = False
        data["paused_until"] = None
        self.save(data)

    def set_paused(self, paused: bool) -> None:
        self.pause(paused, paused)


class EventLog:
    def __init__(self, path: Path) -> None:
        self.path = path

    def write(self, **fields: Any) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        event = {"ts": datetime.now(UTC).isoformat(), **fields}
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, separators=(",", ":"), ensure_ascii=False) + "\n")

    def read(self) -> list[dict[str, Any]]:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        events: list[dict[str, Any]] = []
        for line in lines:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                events.append(value)
        return events

    def tail(self, follow: bool) -> None:
        try:
            handle = self.path.open("r", encoding="utf-8")
        except FileNotFoundError:
            if not follow:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.touch()
            handle = self.path.open("r", encoding="utf-8")
        with handle:
            if follow:
                handle.seek(0, os.SEEK_END)
            while True:
                line = handle.readline()
                if line:
                    print(line.rstrip())
                    continue
                if not follow:
                    return
                time.sleep(0.5)
