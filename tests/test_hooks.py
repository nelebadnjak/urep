from __future__ import annotations

from pathlib import Path

from urep.hooks import normalize_commands, run_hooks


def test_normalize_commands() -> None:
    assert normalize_commands([["sh", "-c", "true"], ["echo", "ok"], "bad", []]) == [
        ["sh", "-c", "true"],
        ["echo", "ok"],
    ]
    assert normalize_commands("not-a-list") == []


def test_run_hooks_success(tmp_path: Path) -> None:
    result = run_hooks(
        [["sh", "-c", "exit 0"]],
        env={"UREP_PATH": "/tmp/x"},
        timeout_s=2,
        cwd=tmp_path,
    )
    assert result.ok
    assert result.code == 0


def test_run_hooks_failure_stops(tmp_path: Path) -> None:
    result = run_hooks(
        [["sh", "-c", "exit 7"], ["sh", "-c", "exit 0"]],
        env={},
        timeout_s=2,
        cwd=tmp_path,
    )
    assert result.code == 7
    assert not result.ok


def test_run_hooks_timeout(tmp_path: Path) -> None:
    result = run_hooks(
        [["sh", "-c", "sleep 5"]],
        env={},
        timeout_s=0.1,
        cwd=tmp_path,
    )
    assert result.code == 124
