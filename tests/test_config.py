from __future__ import annotations

from pathlib import Path

import pytest

from urep.config import (
    Config,
    ConfigError,
    make_node_id,
    parse_duration_ms,
    parse_duration_s,
    parse_mode,
    parse_size,
    sanitize_prefix,
)


def _state_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("UREP_STATE_DIR", str(tmp_path / "state"))


def test_defaults(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("UREP_TOKEN", "secret")
    _state_env(monkeypatch, tmp_path)
    config = Config.load()
    assert config.tags == ["default"]
    assert config.role == "peer"
    assert config.paths == []
    assert config.accept_roots == [Path("/urep")]
    assert config.listen == "0.0.0.0:7801"
    assert config.max_file_size == 16 * 1024 * 1024
    assert config.dir_mode == 0o755


def test_cli_overrides_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("UREP_TOKEN", "secret")
    monkeypatch.setenv("UREP_TAG", "envtag")
    _state_env(monkeypatch, tmp_path)
    config = Config.load(cli={"tags": ["clitag", "other"]})
    assert config.tags == ["clitag", "other"]


def test_token_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("from-file\n")
    monkeypatch.setenv("UREP_TOKEN_FILE", str(token_file))
    _state_env(monkeypatch, tmp_path)
    config = Config.load()
    assert config.token.get_secret_value() == "from-file"


def test_cli_token_file_beats_env_token(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("cli-file\n")
    monkeypatch.setenv("UREP_TOKEN", "env-token")
    _state_env(monkeypatch, tmp_path)
    config = Config.load(cli={"token_file": token_file})
    assert config.token.get_secret_value() == "cli-file"


def test_token_required(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _state_env(monkeypatch, tmp_path)
    with pytest.raises(ConfigError):
        Config.load()


def test_forbidden_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("UREP_TOKEN", "secret")
    _state_env(monkeypatch, tmp_path)
    with pytest.raises(ConfigError):
        Config.load(cli={"paths": ["/etc/urep/token"]})


def test_path_outside_roots(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("UREP_TOKEN", "secret")
    _state_env(monkeypatch, tmp_path)
    with pytest.raises(ConfigError):
        Config.load(
            cli={
                "paths": [str(tmp_path / "app.yaml")],
                "roots": [str(tmp_path / "allowed")],
            }
        )


def test_empty_tags_with_paths_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("UREP_TOKEN", "secret")
    monkeypatch.setenv("UREP_TAG", "")
    _state_env(monkeypatch, tmp_path)
    with pytest.raises(ConfigError):
        Config.load(cli={"paths": [str(tmp_path / "app.yaml")]})


def test_accept_roots_include_path_parent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("UREP_TOKEN", "secret")
    _state_env(monkeypatch, tmp_path)
    path = tmp_path / "app.yaml"
    config = Config.load(cli={"paths": [str(path)]})
    assert tmp_path in config.accept_roots


def test_slave_role(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("UREP_TOKEN", "secret")
    _state_env(monkeypatch, tmp_path)
    config = Config.load(cli={"slave": True})
    assert config.role == "slave"


def test_invalid_tag(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("UREP_TOKEN", "secret")
    _state_env(monkeypatch, tmp_path)
    with pytest.raises(ConfigError):
        Config.load(cli={"tags": ["bad tag"]})


def test_wildcard_tag_must_be_alone(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("UREP_TOKEN", "secret")
    _state_env(monkeypatch, tmp_path)
    with pytest.raises(ConfigError):
        Config.load(cli={"tags": ["*", "web"]})


def test_duration_and_size_parsing() -> None:
    assert parse_duration_ms("200ms") == 200
    assert parse_duration_ms("2s") == 2000
    assert parse_duration_ms("5m") == 300_000
    assert parse_duration_s("60s") == 60
    assert parse_size("16MiB") == 16 * 1024 * 1024
    assert parse_size("1KiB") == 1024
    assert parse_mode("0640") == 0o640


def test_prefix_and_node_id() -> None:
    node = make_node_id("BG")
    assert node.startswith("bg-")
    assert len(node.split("-")) >= 6
    with pytest.raises(ConfigError):
        sanitize_prefix("---")
