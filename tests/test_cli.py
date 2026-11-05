from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from urep.cli import app

runner = CliRunner()


@pytest.mark.unit
def test_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert "urep 0.1.0" in result.output


@pytest.mark.unit
def test_id_with_prefix() -> None:
    result = runner.invoke(app, ["id", "--prefix", "BG"])
    assert result.exit_code == 0
    assert result.output.strip().startswith("bg-")


@pytest.mark.unit
def test_config_json_redacts_token(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("UREP_TOKEN", "super-secret")
    monkeypatch.setenv("UREP_TAG", "nginx")
    monkeypatch.setenv("UREP_STATE_DIR", str(tmp_path / "state"))
    result = runner.invoke(app, ["config", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["token"] == "****"
    assert data["tags"] == ["nginx"]
    assert "super-secret" not in result.output


@pytest.mark.unit
def test_pause_resume(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("UREP_TOKEN", "secret")
    monkeypatch.setenv("UREP_STATE_DIR", str(tmp_path / "state"))

    paused = runner.invoke(app, ["pause"])
    assert paused.exit_code == 0
    state_file = tmp_path / "state" / "state.json"
    assert json.loads(state_file.read_text())["paused"] is True

    resumed = runner.invoke(app, ["resume"])
    assert resumed.exit_code == 0
    assert json.loads(state_file.read_text())["paused"] is False


@pytest.mark.unit
def test_status_json(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("UREP_TOKEN", "secret")
    monkeypatch.setenv("UREP_STATE_DIR", str(tmp_path / "state"))
    result = runner.invoke(app, ["status", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["role"] == "peer"
    assert data["color"] == "GREEN"
    assert data["commits"] == 0


@pytest.mark.unit
def test_daemon_requires_token() -> None:
    result = runner.invoke(app, [])
    assert result.exit_code == 2
    assert "token is required" in result.output
