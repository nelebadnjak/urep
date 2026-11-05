from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .hlc import HLC, Clock

MIN_GIT = (2, 29)
RECORD_SEP = "\x1e"
FIELD_SEP = "\x1f"


class GitError(RuntimeError):
    pass


@dataclass
class Change:
    path: str  # mirror-relative path, e.g. "urep/app.yaml"
    op: str  # A | M | D
    blob: str | None
    mode: int
    version: HLC
    pre_hooks: list[list[str]] = field(default_factory=list)
    post_hooks: list[list[str]] = field(default_factory=list)


@dataclass
class FileState:
    path: str
    blob: str | None
    version: HLC
    mode: int
    deleted: bool
    pre_hooks: list[list[str]]
    post_hooks: list[list[str]]
    commit: str
    ts: int


def blob_hash(data: bytes) -> str:
    digest = hashlib.sha256()
    digest.update(b"blob " + str(len(data)).encode() + b"\0")
    digest.update(data)
    return digest.hexdigest()


def message_id(kind: str, path: str, version: HLC, blob: str | None) -> str:
    digest = hashlib.sha256()
    digest.update(f"{kind}|{path}|{version}|{blob or ''}".encode())
    return digest.hexdigest()


def git_version() -> tuple[int, int]:
    try:
        output = subprocess.check_output(["git", "version"], stderr=subprocess.STDOUT)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise GitError(f"git is not available: {exc}") from exc
    match = re.search(r"(\d+)\.(\d+)", output.decode(errors="replace"))
    if not match:
        raise GitError("cannot parse git version")
    return int(match.group(1)), int(match.group(2))


def parse_urep_data(body: str) -> dict | None:
    for line in body.splitlines():
        if line.startswith("Urep-Data: "):
            try:
                return json.loads(line[len("Urep-Data: ") :])
            except json.JSONDecodeError:
                return None
    return None


class GitRepo:
    def __init__(self, repo_dir: Path) -> None:
        self.repo_dir = repo_dir

    def _run(
        self,
        *args: str,
        input: bytes | str | None = None,
        env: dict[str, str] | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess:
        if isinstance(input, str):
            input = input.encode()
        full_env = os.environ.copy()
        if env:
            full_env.update(env)
        proc = subprocess.run(
            ["git", "-C", str(self.repo_dir), *args],
            input=input,
            capture_output=True,
            env=full_env,
            check=False,
        )
        if check and proc.returncode != 0:
            raise GitError(
                f"git {' '.join(args)} failed: {proc.stderr.decode(errors='replace').strip()}"
            )
        return proc

    def ensure(self) -> None:
        version = git_version()
        if version < MIN_GIT:
            raise GitError(
                f"git >= {MIN_GIT[0]}.{MIN_GIT[1]} required, "
                f"found {version[0]}.{version[1]}"
            )
        self.repo_dir.mkdir(parents=True, exist_ok=True)
        if not (self.repo_dir / ".git").exists():
            proc = self._run(
                "init",
                "--object-format=sha256",
                "--initial-branch=main",
                check=False,
            )
            if proc.returncode != 0:
                raise GitError(
                    "git init --object-format=sha256 failed: "
                    + proc.stderr.decode(errors="replace").strip()
                )
        self._run("config", "user.name", "uRep")
        self._run("config", "user.email", "urep@localhost")
        self._run("config", "commit.gpgsign", "false")
        fmt = self.object_format()
        if fmt != "sha256":
            raise GitError(f"repository must use sha256 object format, found {fmt!r}")

    def object_format(self) -> str:
        proc = self._run("rev-parse", "--show-object-format", check=False)
        if proc.returncode != 0:
            return "unknown"
        return proc.stdout.decode().strip()

    def head(self) -> str | None:
        proc = self._run("rev-parse", "--verify", "HEAD", check=False)
        if proc.returncode != 0:
            return None
        return proc.stdout.decode().strip()

    def commit_count(self) -> int:
        proc = self._run("rev-list", "--count", "HEAD", check=False)
        if proc.returncode != 0:
            return 0
        return int(proc.stdout.decode().strip() or 0)

    def write_mirror(self, rel: str, data: bytes, mode: int) -> None:
        target = self.repo_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".urep.tmp", dir=target.parent
        )
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp, mode)
            os.replace(tmp, target)
            dir_fd = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        finally:
            if tmp.exists():
                tmp.unlink(missing_ok=True)

    def remove_mirror(self, rel: str) -> None:
        (self.repo_dir / rel).unlink(missing_ok=True)

    def commit(
        self,
        *,
        node: str,
        msg_id: str,
        changes: list[Change],
        timestamp_ms: int,
        force: bool = False,
    ) -> str | None:
        if not changes:
            return None
        rels = [change.path for change in changes]
        self._run("add", "-A", "--", *rels)
        diff = self._run("diff", "--cached", "--quiet", "--", *rels, check=False)
        if diff.returncode == 0 and not force:
            return None
        if diff.returncode not in (0, 1):
            raise GitError(diff.stderr.decode(errors="replace").strip())

        stamp = datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC)
        iso = stamp.isoformat().replace("+00:00", "Z")
        lines = [
            f"urep: {len(changes)} change(s) from {node}",
            "",
            f"Urep-Node: {node}",
            f"Urep-Msg: {msg_id}",
            f"Urep-Time: {iso}",
            "Urep-Summary:",
        ]
        for change in changes:
            lines.append(f"  {change.op} /{change.path}")
        payload = {
            "changes": [
                {
                    "p": change.path,
                    "o": change.op,
                    "h": change.blob,
                    "m": f"{change.mode:04o}",
                    "v": str(change.version),
                    "hp": change.pre_hooks,
                    "ho": change.post_hooks,
                }
                for change in changes
            ]
        }
        lines.append("Urep-Data: " + json.dumps(payload, separators=(",", ":"), ensure_ascii=False))
        message = "\n".join(lines) + "\n"

        env = {
            "GIT_AUTHOR_NAME": node,
            "GIT_AUTHOR_EMAIL": f"{node}@urep",
            "GIT_COMMITTER_NAME": node,
            "GIT_COMMITTER_EMAIL": f"{node}@urep",
        }
        args = ["commit", "--no-gpg-sign"]
        if force and diff.returncode == 0:
            args.append("--allow-empty")
        args.extend(["-F", "-"])
        self._run(*args, input=message, env=env)
        return self.head()

    def derive_state(self) -> dict[str, FileState]:
        proc = self._run(
            "log",
            f"--format=%H{FIELD_SEP}%ct{FIELD_SEP}%B{RECORD_SEP}",
            check=False,
        )
        if proc.returncode != 0:
            return {}
        state: dict[str, FileState] = {}
        for record in proc.stdout.decode(errors="replace").split(RECORD_SEP):
            record = record.strip("\n")
            if not record:
                continue
            parts = record.split(FIELD_SEP, 2)
            if len(parts) != 3:
                continue
            sha, committed, body = parts
            data = parse_urep_data(body)
            if not data:
                continue
            for change in data.get("changes", []):
                path = change.get("p")
                if not path or path in state:
                    continue
                try:
                    version = HLC.parse(change["v"])
                    mode = int(change["m"], 8)
                except (KeyError, ValueError):
                    continue
                state[path] = FileState(
                    path=path,
                    blob=change.get("h"),
                    version=version,
                    mode=mode,
                    deleted=change.get("o") == "D",
                    pre_hooks=change.get("hp") or [],
                    post_hooks=change.get("ho") or [],
                    commit=sha.strip(),
                    ts=int(committed),
                )
        return state

    def log(self, rel: str | None, n: int) -> str:
        args = ["log", "--oneline", f"-n{n}"]
        if rel:
            args.extend(["--", rel])
        proc = self._run(*args, check=False)
        return proc.stdout.decode(errors="replace")

    def show(self, rev: str) -> str:
        proc = self._run("show", "--stat", "--patch", rev, check=False)
        if proc.returncode != 0:
            raise GitError(proc.stderr.decode(errors="replace").strip())
        return proc.stdout.decode(errors="replace")

    def diff(self, rev: str, rel: str | None) -> str:
        args = ["show", "--format=fuller", "--patch", rev]
        if rel:
            args.extend(["--", rel])
        proc = self._run(*args, check=False)
        if proc.returncode != 0:
            raise GitError(proc.stderr.decode(errors="replace").strip())
        return proc.stdout.decode(errors="replace")

    def revert(self, rev: str, *, node: str, clock: Clock) -> str | None:
        proc = self._run("show", "-s", "--format=%B", rev, check=False)
        if proc.returncode != 0:
            raise GitError(f"unknown revision: {rev}")
        data = parse_urep_data(proc.stdout.decode(errors="replace"))
        if not data:
            raise GitError(f"{rev} is not a uRep commit")
        parent = f"{rev}^"
        changes: list[Change] = []
        for item in data.get("changes", []):
            rel = item["p"]
            mode = int(item.get("m", "0644"), 8)
            old = self._run("show", f"{parent}:{rel}", check=False)
            version = clock.tick()
            if old.returncode == 0:
                content = old.stdout
                self.write_mirror(rel, content, mode)
                changes.append(
                    Change(rel, "M", blob_hash(content), mode, version)
                )
            else:
                self.remove_mirror(rel)
                changes.append(Change(rel, "D", None, mode, version))
        return self.commit(
            node=node,
            msg_id=message_id("revert", rev, clock.tick(), None),
            changes=changes,
            timestamp_ms=int(time.time() * 1000),
        )

    def gc(self) -> str:
        proc = self._run("gc", "--auto", check=False)
        return proc.stderr.decode(errors="replace")

    def conflicts(self) -> list[str]:
        proc = self._run(
            "for-each-ref",
            "--format=%(refname) %(objectname:short)",
            "refs/urep/conflict",
            check=False,
        )
        return [line for line in proc.stdout.decode().splitlines() if line.strip()]

    def restore(self, ref: str, *, node: str, clock: Clock) -> str | None:
        resolved = self._run("rev-parse", "--verify", ref, check=False)
        if resolved.returncode != 0:
            raise GitError(f"unknown ref: {ref}")
        sha = resolved.stdout.decode().strip()
        body = self._run("show", "-s", "--format=%B", sha).stdout.decode(errors="replace")
        data = parse_urep_data(body)
        if not data:
            raise GitError(f"{ref} is not a uRep commit")
        changes: list[Change] = []
        for item in data.get("changes", []):
            rel = item["p"]
            mode = int(item.get("m", "0644"), 8)
            content = self._run("show", f"{sha}:{rel}", check=False)
            version = clock.tick()
            if content.returncode == 0:
                self.write_mirror(rel, content.stdout, mode)
                changes.append(Change(rel, "M", blob_hash(content.stdout), mode, version))
            else:
                self.remove_mirror(rel)
                changes.append(Change(rel, "D", None, mode, version))
        return self.commit(
            node=node,
            msg_id=message_id("restore", ref, clock.tick(), None),
            changes=changes,
            timestamp_ms=int(time.time() * 1000),
        )
