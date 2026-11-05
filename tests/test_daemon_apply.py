from __future__ import annotations

import stat
from pathlib import Path

import pytest

from urep.config import Config
from urep.daemon import Daemon
from urep.gitrepo import blob_hash
from urep.hlc import HLC
from urep.paths import mirror_rel


def _daemon(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, roots: Path | None = None
) -> tuple[Daemon, Path]:
    root = roots or (tmp_path / "allowed")
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("UREP_TOKEN", "test-token")
    monkeypatch.setenv("UREP_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("UREP_ACCEPT_ROOTS", str(root))
    monkeypatch.setenv("UREP_TAG", "test")
    monkeypatch.setenv("UREP_HOOK_TIMEOUT", "2s")
    return Daemon(Config.load()), root


def _payload(
    path: Path,
    data: bytes | None,
    version: HLC,
    **overrides: object,
) -> dict:
    payload: dict = {
        "path": str(path),
        "version": str(version),
        "blob": blob_hash(data) if data is not None else None,
        "mode": "0640",
        "tags": ["test"],
        "hp": [],
        "ho": [],
        "data": data,
        "msg_id": f"msg-{version}",
        "node_id": "peer",
    }
    payload.update(overrides)
    return payload


def _prime(daemon: Daemon, path: Path, data: bytes = b"one\n") -> HLC:
    version = HLC(1000, 0, "peer")
    ok, reason = daemon._apply_remote(_payload(path, data, version), "peer")
    assert ok and reason == "ok"
    return version


def test_apply_creates_file_and_commit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    daemon, root = _daemon(monkeypatch, tmp_path)
    path = root / "app.yaml"
    version = _prime(daemon, path)
    assert path.read_bytes() == b"one\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    item = daemon.state[mirror_rel(path)]
    assert item.version == version
    assert item.blob == blob_hash(b"one\n")
    assert item.deleted is False


def test_older_version_is_noop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    daemon, root = _daemon(monkeypatch, tmp_path)
    path = root / "app.yaml"
    _prime(daemon, path)
    ok, reason = daemon._apply_remote(
        _payload(path, b"old\n", HLC(500, 0, "peer")), "peer"
    )
    assert ok and reason == "noop"
    assert path.read_bytes() == b"one\n"


def test_newer_version_updates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    daemon, root = _daemon(monkeypatch, tmp_path)
    path = root / "app.yaml"
    _prime(daemon, path)
    ok, reason = daemon._apply_remote(
        _payload(path, b"two\n", HLC(2000, 0, "peer")), "peer"
    )
    assert ok and reason == "ok"
    assert path.read_bytes() == b"two\n"


def test_tag_mismatch_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    daemon, root = _daemon(monkeypatch, tmp_path)
    path = root / "app.yaml"
    ok, reason = daemon._apply_remote(
        _payload(path, b"one\n", HLC(1000, 0, "peer"), tags=["other"]), "peer"
    )
    assert not ok
    assert reason == "tag_mismatch"
    assert not path.exists()


def test_path_outside_roots_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    outside.mkdir()
    daemon, _ = _daemon(monkeypatch, tmp_path, roots=allowed)
    path = outside / "app.yaml"
    ok, reason = daemon._apply_remote(
        _payload(path, b"one\n", HLC(1000, 0, "peer")), "peer"
    )
    assert not ok
    assert reason == "path_not_allowed"
    assert not path.exists()


def test_pre_hook_failure_aborts_write(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    daemon, root = _daemon(monkeypatch, tmp_path)
    path = root / "app.yaml"
    _prime(daemon, path)
    ok, reason = daemon._apply_remote(
        _payload(
            path,
            b"two\n",
            HLC(2000, 0, "peer"),
            hp=[["sh", "-c", "exit 3"]],
        ),
        "peer",
    )
    assert not ok
    assert reason == "pre_hook_failed"
    assert path.read_bytes() == b"one\n"


def test_post_hook_failure_keeps_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    daemon, root = _daemon(monkeypatch, tmp_path)
    path = root / "app.yaml"
    _prime(daemon, path)
    ok, reason = daemon._apply_remote(
        _payload(
            path,
            b"two\n",
            HLC(2000, 0, "peer"),
            ho=[["sh", "-c", "exit 3"]],
        ),
        "peer",
    )
    assert ok
    assert reason == "hook_failed"
    assert path.read_bytes() == b"two\n"


def test_delete_applies_tombstone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    daemon, root = _daemon(monkeypatch, tmp_path)
    path = root / "app.yaml"
    _prime(daemon, path)
    ok, reason = daemon._apply_remote(
        _payload(
            path,
            None,
            HLC(2000, 0, "peer"),
            deleted=True,
            blob=blob_hash(b"one\n"),
        ),
        "peer",
    )
    assert ok and reason == "ok"
    assert not path.exists()
    assert daemon.state[mirror_rel(path)].deleted is True


def test_mode_only_change_is_committed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    daemon, root = _daemon(monkeypatch, tmp_path)
    path = root / "app.yaml"
    _prime(daemon, path)
    ok, reason = daemon._apply_remote(
        _payload(path, b"one\n", HLC(2000, 0, "peer"), mode="0600"), "peer"
    )
    assert ok and reason == "ok"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert daemon.state[mirror_rel(path)].mode == 0o600


def test_first_join_snapshots_local_before_remote(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    daemon, root = _daemon(monkeypatch, tmp_path)
    path = root / "app.yaml"
    path.write_bytes(b"local\n")
    ok, reason = daemon._apply_remote(
        _payload(path, b"remote\n", HLC(1000, 0, "peer")), "peer"
    )
    assert ok and reason == "ok"
    assert path.read_bytes() == b"remote\n"
    assert daemon.repo.commit_count() == 2
    state = daemon.repo.derive_state()
    item = state[mirror_rel(path)]
    assert item.blob == blob_hash(b"remote\n")
    assert item.version == HLC(1000, 0, "peer")
