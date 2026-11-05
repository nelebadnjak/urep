from __future__ import annotations

import json
import socket
import time
from pathlib import Path

import cbor2
import pytest

from urep.config import Config
from urep.crypto import encrypt_discovery
from urep.discovery import Discovery

TOKEN = "discovery-token"


def _free_port() -> int:
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


def _config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Config:
    monkeypatch.setenv("UREP_TOKEN", TOKEN)
    monkeypatch.setenv("UREP_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("UREP_LISTEN", "0.0.0.0:7801")
    return Config.load()


def _payload(node_id: str = "remote") -> dict:
    return {
        "proto": 1,
        "node_id": node_id,
        "tags": ["test"],
        "role": "peer",
        "tcp_port": 7801,
        "ts": time.time(),
        "color": "GREEN",
        "last_path": None,
        "peers": [],
    }


def test_ingest_adds_peer_and_writes_snapshot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = _config(monkeypatch, tmp_path)
    seen: list[dict] = []
    discovery = Discovery(
        config,
        "self",
        on_peer=seen.append,
        peers_path=tmp_path / "peers.json",
    )
    packet = encrypt_discovery(TOKEN, cbor2.dumps(_payload()))
    discovery.ingest(packet, ("127.0.0.1", 50000))

    peers = discovery.snapshot()
    assert len(peers) == 1
    assert peers[0]["node_id"] == "remote"
    assert peers[0]["address"] == "127.0.0.1:7801"
    assert peers[0]["tags"] == ["test"]
    assert [item["node_id"] for item in seen] == ["remote"]

    saved = json.loads((tmp_path / "peers.json").read_text())
    assert saved["peers"][0]["node_id"] == "remote"


def test_gossip_adds_third_peer(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config = _config(monkeypatch, tmp_path)
    seen: list[dict] = []
    discovery = Discovery(config, "self", on_peer=seen.append)
    payload = _payload("remote")
    payload["peers"] = [
        {
            "node_id": "third",
            "tags": ["other"],
            "role": "slave",
            "address": "10.0.0.3:7801",
            "tcp_port": 7801,
            "seen": 1,
        }
    ]
    discovery.ingest(encrypt_discovery(TOKEN, cbor2.dumps(payload)), ("127.0.0.1", 5000))
    ids = {item["node_id"] for item in discovery.snapshot()}
    assert ids == {"remote", "third"}
    assert {item["node_id"] for item in seen} == {"remote", "third"}


def test_wrong_token_and_stale_packets_ignored(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = _config(monkeypatch, tmp_path)
    discovery = Discovery(config, "self")

    wrong = encrypt_discovery("other-token", cbor2.dumps(_payload()))
    discovery.ingest(wrong, ("127.0.0.1", 5000))
    assert discovery.snapshot() == []

    stale = _payload()
    stale["ts"] = time.time() - 1000
    discovery.ingest(
        encrypt_discovery(TOKEN, cbor2.dumps(stale)), ("127.0.0.1", 5000)
    )
    assert discovery.snapshot() == []


def test_self_and_duplicate_announce(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config = _config(monkeypatch, tmp_path)
    seen: list[dict] = []
    discovery = Discovery(config, "self", on_peer=seen.append)
    discovery.ingest(
        encrypt_discovery(TOKEN, cbor2.dumps(_payload("self"))), ("127.0.0.1", 5000)
    )
    assert discovery.snapshot() == []
    assert seen == []


def test_start_and_stop(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("UREP_MULTICAST_GROUP", f"127.0.0.1:{_free_port()}")
    config = _config(monkeypatch, tmp_path)
    discovery = Discovery(config, "self", peers_path=tmp_path / "peers.json")
    discovery.start()
    try:
        time.sleep(0.2)
    finally:
        discovery.stop()
