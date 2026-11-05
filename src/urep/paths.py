from __future__ import annotations

import os
from pathlib import Path

HARD_DENY = (
    Path("/etc/urep"),
    Path("/var/lib/urep"),
    Path("/proc"),
    Path("/sys"),
    Path("/dev"),
    Path("/run"),
)


class PathError(ValueError):
    pass


def normalize(raw: str | Path) -> Path:
    path = Path(raw)
    if not path.is_absolute():
        raise PathError(f"path must be absolute: {raw}")
    if ".." in path.parts:
        raise PathError(f"path must not contain '..': {raw}")
    return Path(os.path.normpath(str(path)))


def mirror_rel(path: Path) -> str:
    rel = str(path).lstrip("/")
    if not rel:
        raise PathError(f"cannot mirror root path: {path}")
    if ".." in Path(rel).parts:
        raise PathError(f"path must not contain '..': {path}")
    return rel


def mirror_path(repo_dir: Path, path: Path) -> Path:
    return repo_dir / mirror_rel(path)


def is_under(path: Path, roots: list[Path]) -> bool:
    for root in roots:
        if path == root:
            return True
        if root.exists() and root.is_file():
            continue
        if path.is_relative_to(root):
            return True
    return False


def real_parent_allowed(path: Path, roots: list[Path]) -> bool:
    """Reject writes whose parent resolves outside the allowlist (symlink escape)."""
    if any(path == root for root in roots):
        return True
    parent = Path(os.path.realpath(path.parent))
    for root in roots:
        if root.exists() and root.is_file():
            continue
        real_root = Path(os.path.realpath(root))
        if parent == real_root or parent.is_relative_to(real_root):
            return True
    return False


def is_forbidden(path: Path, extra: list[Path]) -> bool:
    for root in (*HARD_DENY, *extra):
        if path == root or path.is_relative_to(root):
            return True
    return False


def tag_match(message: list[str], node: list[str]) -> bool:
    if "*" in message or "*" in node:
        return True
    return bool(set(message) & set(node))


def ensure_parent(path: Path, mode: int) -> None:
    if path.parent.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True, mode=mode)
