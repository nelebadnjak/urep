from __future__ import annotations

import subprocess
from pathlib import Path

from urep.gitrepo import Change, GitRepo, blob_hash
from urep.hlc import HLC, Clock


def _repo(tmp_path: Path) -> GitRepo:
    repo = GitRepo(tmp_path / "repo")
    repo.ensure()
    return repo


def _commit(
    repo: GitRepo,
    rel: str,
    data: bytes | None,
    mode: int,
    version: HLC,
    *,
    op: str = "A",
    pre: list[list[str]] | None = None,
    post: list[list[str]] | None = None,
    force: bool = False,
    old_blob: str | None = None,
) -> str | None:
    if data is None:
        repo.remove_mirror(rel)
        blob = old_blob
    else:
        repo.write_mirror(rel, data, mode)
        blob = blob_hash(data)
    return repo.commit(
        node="node",
        msg_id=f"msg-{version}",
        changes=[Change(rel, op, blob, mode, version, pre or [], post or [])],
        timestamp_ms=version.wall_ms,
        force=force,
    )


def test_repo_is_sha256(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    assert repo.object_format() == "sha256"


def test_blob_hash_matches_git(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    data = b"hello\n"
    proc = subprocess.run(
        ["git", "-C", str(repo.repo_dir), "hash-object", "--stdin"],
        input=data,
        capture_output=True,
        check=True,
    )
    assert proc.stdout.decode().strip() == blob_hash(data)


def test_commit_and_derive_state(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    version = HLC(1000, 0, "node")
    data = b"one\n"
    sha = _commit(
        repo,
        "urep/app.yaml",
        data,
        0o640,
        version,
        post=[["nginx", "-s", "reload"]],
    )
    assert sha is not None

    state = repo.derive_state()
    item = state["urep/app.yaml"]
    assert item.blob == blob_hash(data)
    assert item.version == version
    assert item.mode == 0o640
    assert item.deleted is False
    assert item.post_hooks == [["nginx", "-s", "reload"]]
    assert item.commit == sha


def test_newest_commit_wins(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    first = HLC(1000, 0, "node")
    second = HLC(2000, 0, "node")
    _commit(repo, "urep/app.yaml", b"one\n", 0o644, first)
    _commit(repo, "urep/app.yaml", b"two\n", 0o644, second, op="M")
    state = repo.derive_state()
    assert state["urep/app.yaml"].version == second
    assert state["urep/app.yaml"].blob == blob_hash(b"two\n")


def test_delete_is_tombstone(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    first = HLC(1000, 0, "node")
    second = HLC(2000, 0, "node")
    _commit(repo, "urep/app.yaml", b"one\n", 0o644, first)
    _commit(
        repo,
        "urep/app.yaml",
        None,
        0o644,
        second,
        op="D",
        old_blob=blob_hash(b"one\n"),
    )
    state = repo.derive_state()
    assert state["urep/app.yaml"].deleted is True
    assert state["urep/app.yaml"].version == second
    assert state["urep/app.yaml"].blob == blob_hash(b"one\n")


def test_mode_only_change_needs_force(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    first = HLC(1000, 0, "node")
    second = HLC(2000, 0, "node")
    _commit(repo, "urep/app.yaml", b"one\n", 0o644, first)
    sha = _commit(repo, "urep/app.yaml", b"one\n", 0o600, second, op="M", force=True)
    assert sha is not None
    state = repo.derive_state()
    assert state["urep/app.yaml"].mode == 0o600
    assert state["urep/app.yaml"].version == second


def test_noop_commit_returns_none(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    first = HLC(1000, 0, "node")
    _commit(repo, "urep/app.yaml", b"one\n", 0o644, first)
    again = _commit(repo, "urep/app.yaml", b"one\n", 0o644, HLC(2000, 0, "node"), op="M")
    assert again is None


def test_revert_restores_parent_content(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    first = HLC(1000, 0, "node")
    second = HLC(2000, 0, "node")
    _commit(repo, "urep/app.yaml", b"one\n", 0o644, first)
    target = _commit(repo, "urep/app.yaml", b"two\n", 0o644, second, op="M")
    assert target is not None

    reverted = repo.revert(target, node="node", clock=Clock("node"))
    assert reverted is not None
    state = repo.derive_state()
    assert state["urep/app.yaml"].blob == blob_hash(b"one\n")
    assert (repo.repo_dir / "urep/app.yaml").read_bytes() == b"one\n"


def test_commit_message_has_machine_data(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    sha = _commit(repo, "urep/app.yaml", b"one\n", 0o644, HLC(1000, 0, "node"))
    assert sha is not None
    body = subprocess.run(
        ["git", "-C", str(repo.repo_dir), "show", "-s", "--format=%B", sha],
        capture_output=True,
        check=True,
    ).stdout.decode()
    assert "Urep-Node: node" in body
    assert "Urep-Data: " in body
    assert "Urep-Summary:" in body
