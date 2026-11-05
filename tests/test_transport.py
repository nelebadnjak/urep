from __future__ import annotations

import asyncio
import socket
from pathlib import Path

import pytest

from urep.config import Config
from urep.protocol import Msg, read_secure
from urep.transport import Transport, TransportError, TransportService, ping_peer

TOKEN = "test-token"


def _free_port() -> int:
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


def test_handshake_and_ping() -> None:
    async def scenario() -> None:
        server = Transport(TOKEN, "srv", ["test"], "peer")
        await server.start("127.0.0.1", 0)
        port = server.bound_port
        assert port is not None

        client = Transport(TOKEN, "cli", ["test"], "peer")
        try:
            peer = await client.connect("127.0.0.1", port)
            assert peer.node_id == "srv"
            assert peer.tags == ["test"]
            assert peer.role == "peer"
            assert client.peers["srv"] is peer

            await client.send(peer, Msg.PING, {"ts": 123})
            msg_type, payload = await asyncio.wait_for(
                read_secure(peer.reader, peer.key), timeout=3
            )
            assert msg_type == Msg.PONG
            assert payload["ts"] == 123
            assert payload["node_id"] == "srv"
        finally:
            await client.stop()
            await server.stop()

    asyncio.run(scenario())


def test_wrong_token_is_rejected() -> None:
    async def scenario() -> None:
        server = Transport(TOKEN, "srv", ["test"], "peer")
        await server.start("127.0.0.1", 0)
        port = server.bound_port
        assert port is not None

        client = Transport("wrong-token", "cli", ["test"], "peer")
        try:
            with pytest.raises(TransportError):
                await client.connect("127.0.0.1", port)
        finally:
            await client.stop()
            await server.stop()

    asyncio.run(scenario())


def test_ping_peer_through_service(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("UREP_TOKEN", TOKEN)
    monkeypatch.setenv("UREP_TAG", "test")
    monkeypatch.setenv("UREP_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("UREP_LISTEN", f"127.0.0.1:{_free_port()}")
    config = Config.load()

    service = TransportService(config, "srv")
    service.start()
    try:
        result = ping_peer(
            f"127.0.0.1:{service.bound_port}",
            token=TOKEN,
            node_id="cli",
            tags=["test"],
            role="peer",
        )
    finally:
        service.stop()

    assert result["node_id"] == "srv"
    assert result["role"] == "peer"
    assert result["tags"] == ["test"]
    assert result["rtt_ms"] >= 0
