from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class HookResult:
    code: int
    output: str
    ms: int

    @property
    def ok(self) -> bool:
        return self.code == 0


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def normalize_commands(value: Any) -> list[list[str]]:
    if not isinstance(value, list):
        return []
    commands: list[list[str]] = []
    for item in value:
        if (
            isinstance(item, list)
            and item
            and all(isinstance(part, str) for part in item)
        ):
            commands.append(item)
    return commands


def run_hooks(
    commands: list[list[str]],
    *,
    env: dict[str, str],
    timeout_s: float,
    cwd: Path,
) -> HookResult:
    started = time.monotonic()
    if not commands:
        return HookResult(0, "", 0)
    full_env = os.environ.copy()
    full_env.update(env)
    output = ""
    for command in commands:
        try:
            proc = subprocess.run(
                command,
                cwd=str(cwd),
                env=full_env,
                capture_output=True,
                timeout=timeout_s,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return HookResult(124, f"timeout: {command[0]}", _elapsed_ms(started))
        except OSError as exc:
            return HookResult(127, str(exc), _elapsed_ms(started))
        raw = (proc.stdout or b"") + (proc.stderr or b"")
        output = raw.decode(errors="replace")[-4000:]
        if proc.returncode != 0:
            return HookResult(proc.returncode, output, _elapsed_ms(started))
    return HookResult(0, output, _elapsed_ms(started))
