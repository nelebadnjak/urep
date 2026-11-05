from __future__ import annotations

import errno
import os
import tempfile
from pathlib import Path


def ensure_parent(path: Path, mode: int) -> None:
    if path.parent.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True, mode=mode)


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _in_place_write(path: Path, data: bytes, mode: int) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(path, mode)


def atomic_write(path: Path, data: bytes, mode: int) -> bool:
    """Write atomically where possible; fall back to in-place for bind mounts.

    Returns True for the atomic rename path and False for the in-place fallback.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".urep.tmp", dir=path.parent
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        try:
            os.replace(tmp, path)
        except OSError as exc:
            if exc.errno not in (errno.EBUSY, errno.EXDEV):
                raise
            tmp.unlink(missing_ok=True)
            _in_place_write(path, data, mode)
            return False
        _fsync_dir(path.parent)
        return True
    finally:
        tmp.unlink(missing_ok=True)
