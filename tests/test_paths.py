from __future__ import annotations

from pathlib import Path

import pytest

from urep.paths import (
    PathError,
    is_forbidden,
    is_under,
    mirror_rel,
    normalize,
    real_parent_allowed,
    tag_match,
)


def test_normalize_rejects_relative() -> None:
    with pytest.raises(PathError):
        normalize("etc/app.yaml")


def test_normalize_rejects_parent_component() -> None:
    with pytest.raises(PathError):
        normalize("/etc/../etc/app.yaml")


def test_mirror_rel() -> None:
    assert mirror_rel(Path("/urep/app.yaml")) == "urep/app.yaml"
    assert mirror_rel(Path("/etc/nginx/nginx.conf")) == "etc/nginx/nginx.conf"


def test_is_under_file_and_directory_roots(tmp_path: Path) -> None:
    file_root = tmp_path / "hosts"
    file_root.write_text("x")
    directory_root = tmp_path / "nginx"
    directory_root.mkdir()
    assert is_under(file_root, [file_root])
    assert not is_under(file_root / "child", [file_root])
    assert is_under(directory_root / "nginx.conf", [directory_root])
    assert not is_under(tmp_path / "other", [directory_root])


def test_real_parent_allowed_rejects_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "link").symlink_to(outside, target_is_directory=True)
    assert not real_parent_allowed(root / "link" / "file", [root])
    assert real_parent_allowed(root / "sub" / "file", [root])


def test_forbidden_paths(tmp_path: Path) -> None:
    assert is_forbidden(Path("/etc/urep/token"), [])
    assert is_forbidden(Path("/proc/1/environ"), [])
    assert not is_forbidden(tmp_path / "app.yaml", [])


def test_tag_match() -> None:
    assert tag_match(["a"], ["a", "b"])
    assert tag_match(["*"], ["a"])
    assert tag_match(["a"], ["*"])
    assert not tag_match(["a"], ["b"])
    assert not tag_match([], ["a"])
