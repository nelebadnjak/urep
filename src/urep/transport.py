from __future__ import annotations

import asyncio
import inspect
import logging
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .config import Config
from .crypto import (
    CryptoError,
    client_proof,
    derive_session_key,
    server_proof,
    verify,
)
from .paths import tag_match
from .protocol import (
    PROTO_VERSION,
    Msg,
    ProtocolError,
    msg_name,
    read_plain,
    read_secure,
    write_plain,
)

log = logging.getLogger("urep.transport")

HANDSHAKE_TIMEOUT = 5.0
NONCE_LEN = 32


class TransportError(RuntimeError):
    pass


@dataclass
class Peer:
    node_id: str
    tags: list[str]
    role: str
    address: str
    key: bytes
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


def parse_address(address: str) -> tuple[str, int]:
    host, sep, port = address.rpartition(":")
    if not sep or not host or not port.isdigit():
        raise TransportError(f"invalid address: {address!r} (expected host:port)")
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    value = int(port)
    if not 1 <= value <= 65535:
        raise TransportError(f"invalid port: {port}")
    return host, value


def _client_address(writer: asyncio.StreamWriter) -> str:
    peer = writer.get_extra_info("peername")
    if isinstance(peer, tuple) and len(peer) >= 2:
        return f"{peer[0]}:{peer[1]}"
    return str(peer)


class Transport:
    def __init__(
        self,
        token: str,
        node_id: str,
        tags: list[str],
        role: str,
        on_message: Callable[[Peer, int, dict[str, Any]], Any] | None = None,
    ) -> None:
        self.token = token
        self.node_id = node_id
        self.tags = tags
        self.role = role
        self.on_message = on_message or self._default_on_message
        self.peers: dict[str, Peer] = {}
        self.bound_port: int | None = None
        self._server: asyncio.AbstractServer | None = None
        self._tasks: set[asyncio.Task] = set()
        self._stopped = False

    @staticmethod
    def _default_on_message(peer: Peer, msg_type: int, payload: dict[str, Any]) -> None:
        log.debug("message %s from %s: %s", msg_name(msg_type), peer.node_id, payload)

    async def start(self, host: str, port: int) -> None:
        self._server = await asyncio.start_server(self._handle_client, host, port)
        if self._server.sockets:
            self.bound_port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        for peer in list(self.peers.values()):
            await self._close_peer(peer)
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def connect(
        self,
        host: str,
        port: int,
        timeout: float = HANDSHAKE_TIMEOUT,
        start_reader: bool = False,
    ) -> Peer:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout
            )
        except (TimeoutError, OSError) as exc:
            raise TransportError(f"cannot connect to {host}:{port}: {exc}") from exc
        try:
            peer = await asyncio.wait_for(
                self._client_handshake(reader, writer, f"{host}:{port}"), timeout
            )
        except Exception:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            raise
        previous = self.peers.get(peer.node_id)
        if peer.node_id == self.node_id:
            await self._close_writer(peer.writer)
            raise TransportError("cannot connect to self")
        if previous is not None:
            await self._close_peer(previous)
        self.peers[peer.node_id] = peer
        if start_reader:
            task = asyncio.create_task(self._client_read_loop(peer))
            self._tasks.add(task)
        return peer

    async def _client_read_loop(self, peer: Peer) -> None:
        task = asyncio.current_task()
        try:
            await self._read_loop(peer)
        except (
            asyncio.IncompleteReadError,
            TimeoutError,
            ConnectionError,
            OSError,
            CryptoError,
            ProtocolError,
            TransportError,
        ) as exc:
            log.debug("client connection to %s closed: %s", peer.node_id, exc)
        except Exception:
            log.exception("client connection error")
        finally:
            self.peers.pop(peer.node_id, None)
            await self._close_writer(peer.writer)
            if task is not None:
                self._tasks.discard(task)

    async def send(self, peer: Peer, msg_type: int, payload: dict[str, Any]) -> None:
        frame = encode_secure_frame(msg_type, payload, peer.key)
        async with peer.send_lock:
            peer.writer.write(frame)
            await peer.writer.drain()

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peer: Peer | None = None
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)
        try:
            peer = await asyncio.wait_for(
                self._server_handshake(reader, writer), HANDSHAKE_TIMEOUT
            )
            previous = self.peers.get(peer.node_id)
            if previous is not None:
                await self._close_peer(previous)
            self.peers[peer.node_id] = peer
            log.info(
                "peer connected node=%s role=%s addr=%s",
                peer.node_id,
                peer.role,
                peer.address,
            )
            await self._read_loop(peer)
        except (
            TimeoutError,
            asyncio.IncompleteReadError,
            ConnectionError,
            OSError,
            CryptoError,
            ProtocolError,
            TransportError,
        ) as exc:
            log.debug("connection closed: %s", exc)
        except Exception:
            log.exception("unexpected connection error")
        finally:
            if peer is not None:
                self.peers.pop(peer.node_id, None)
            await self._close_writer(writer)
            if task is not None:
                self._tasks.discard(task)

    async def _read_loop(self, peer: Peer) -> None:
        while True:
            msg_type, payload = await read_secure(peer.reader, peer.key)
            if msg_type == Msg.PING:
                await self.send(
                    peer,
                    Msg.PONG,
                    {
                        "ts": payload.get("ts"),
                        "node_id": self.node_id,
                        "tags": self.tags,
                        "role": self.role,
                    },
                )
                continue
            result = self.on_message(peer, msg_type, payload)
            if inspect.isawaitable(result):
                await result

    async def _server_handshake(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> Peer:
        msg_type, hello = await read_plain(reader)
        if msg_type != Msg.HELLO:
            raise TransportError(f"expected HELLO, got {msg_name(msg_type)}")
        if hello.get("proto") != PROTO_VERSION:
            raise TransportError("protocol version mismatch")
        node_id = hello.get("node_id")
        if not isinstance(node_id, str) or not node_id:
            raise TransportError("missing node_id")
        if node_id == self.node_id:
            raise TransportError("duplicate node id")
        client_nonce = hello.get("nonce")
        if not isinstance(client_nonce, bytes) or len(client_nonce) != NONCE_LEN:
            raise TransportError("invalid client nonce")
        tags = hello.get("tags")
        if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
            raise TransportError("invalid tags")
        role = hello.get("role")
        if role not in {"peer", "slave"}:
            raise TransportError("invalid role")

        server_nonce = os.urandom(NONCE_LEN)
        await write_plain(
            writer,
            Msg.HELLO_ACK,
            {
                "proto": PROTO_VERSION,
                "node_id": self.node_id,
                "tags": self.tags,
                "role": self.role,
                "nonce": server_nonce,
                "proof": server_proof(
                    self.token, client_nonce, server_nonce, self.node_id
                ),
            },
        )

        msg_type, auth = await read_plain(reader)
        if msg_type != Msg.AUTH:
            raise TransportError(f"expected AUTH, got {msg_name(msg_type)}")
        received = auth.get("proof")
        if not isinstance(received, bytes):
            raise TransportError("invalid AUTH proof")
        expected = client_proof(self.token, client_nonce, server_nonce, node_id)
        if not verify(expected, received):
            raise TransportError("client authentication failed")

        key = derive_session_key(self.token, client_nonce, server_nonce)
        return Peer(
            node_id=node_id,
            tags=list(tags),
            role=str(role),
            address=_client_address(writer),
            key=key,
            reader=reader,
            writer=writer,
        )

    async def _client_handshake(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, address: str
    ) -> Peer:
        client_nonce = os.urandom(NONCE_LEN)
        await write_plain(
            writer,
            Msg.HELLO,
            {
                "proto": PROTO_VERSION,
                "node_id": self.node_id,
                "tags": self.tags,
                "role": self.role,
                "nonce": client_nonce,
            },
        )
        msg_type, ack = await read_plain(reader)
        if msg_type != Msg.HELLO_ACK:
            raise TransportError(f"expected HELLO_ACK, got {msg_name(msg_type)}")
        if ack.get("proto") != PROTO_VERSION:
            raise TransportError("protocol version mismatch")
        node_id = ack.get("node_id")
        if not isinstance(node_id, str) or not node_id:
            raise TransportError("missing server node_id")
        server_nonce = ack.get("nonce")
        if not isinstance(server_nonce, bytes) or len(server_nonce) != NONCE_LEN:
            raise TransportError("invalid server nonce")
        proof = ack.get("proof")
        if not isinstance(proof, bytes):
            raise TransportError("invalid server proof")
        expected = server_proof(self.token, client_nonce, server_nonce, node_id)
        if not verify(expected, proof):
            raise TransportError("server authentication failed")

        await write_plain(
            writer,
            Msg.AUTH,
            {"proof": client_proof(self.token, client_nonce, server_nonce, self.node_id)},
        )
        key = derive_session_key(self.token, client_nonce, server_nonce)
        tags = ack.get("tags")
        role = ack.get("role")
        return Peer(
            node_id=node_id,
            tags=list(tags) if isinstance(tags, list) else [],
            role=str(role),
            address=address,
            key=key,
            reader=reader,
            writer=writer,
        )

    async def _close_peer(self, peer: Peer) -> None:
        self.peers.pop(peer.node_id, None)
        await self._close_writer(peer.writer)

    @staticmethod
    async def _close_writer(writer: asyncio.StreamWriter) -> None:
        if writer.is_closing():
            return
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


def encode_secure_frame(msg_type: int, payload: dict[str, Any], key: bytes) -> bytes:
    # Local import keeps the hot path explicit and avoids a second protocol module.
    from .protocol import encode_secure

    return encode_secure(msg_type, payload, key)


def ping_peer(
    address: str,
    *,
    token: str,
    node_id: str,
    tags: list[str],
    role: str,
    timeout: float = HANDSHAKE_TIMEOUT,
) -> dict[str, Any]:
    host, port = parse_address(address)

    async def _run() -> dict[str, Any]:
        client = Transport(token=token, node_id=node_id, tags=tags, role=role)
        try:
            peer = await asyncio.wait_for(client.connect(host, port), timeout)
            started = time.perf_counter()
            await client.send(peer, Msg.PING, {"ts": time.time()})
            msg_type, payload = await asyncio.wait_for(
                read_secure(peer.reader, peer.key), timeout
            )
            rtt_ms = (time.perf_counter() - started) * 1000
            if msg_type != Msg.PONG:
                raise TransportError(f"expected PONG, got {msg_name(msg_type)}")
            return {
                "node_id": peer.node_id,
                "tags": peer.tags,
                "role": peer.role,
                "rtt_ms": rtt_ms,
                "ts": payload.get("ts"),
            }
        finally:
            await client.stop()

    try:
        return asyncio.run(_run())
    except (TimeoutError, OSError, ProtocolError, CryptoError) as exc:
        raise TransportError(str(exc)) from exc


class TransportService:
    """Runs the asyncio transport in a background thread for the sync daemon."""

    def __init__(
        self,
        config: Config,
        node_id: str,
        on_message: Callable[[Peer, int, dict[str, Any]], Any] | None = None,
    ) -> None:
        self.config = config
        self.node_id = node_id
        self.on_message = on_message
        self._thread = threading.Thread(target=self._run, name="urep-transport", daemon=True)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._transport: Transport | None = None
        self._ready = threading.Event()
        self._error: Exception | None = None

    def start(self) -> None:
        self._thread.start()
        if not self._ready.wait(10):
            raise TransportError("transport did not start in time")
        if self._error is not None:
            raise TransportError(str(self._error))

    @property
    def bound_port(self) -> int:
        if self._transport is None or self._transport.bound_port is None:
            raise TransportError("transport is not running")
        return self._transport.bound_port

    def connect(self, address: str) -> Peer:
        if self._loop is None or self._transport is None:
            raise TransportError("transport is not running")
        host, port = parse_address(address)
        future = asyncio.run_coroutine_threadsafe(
            self._transport.connect(host, port, start_reader=True), self._loop
        )
        try:
            return future.result(timeout=10)
        except Exception as exc:
            raise TransportError(str(exc)) from exc

    def has_peer(self, node_id: str) -> bool:
        return self._transport is not None and node_id in self._transport.peers

    def peers_snapshot(self) -> list[dict[str, Any]]:
        if self._transport is None:
            return []
        return [
            {
                "node_id": peer.node_id,
                "tags": list(peer.tags),
                "role": peer.role,
                "address": peer.address,
            }
            for peer in list(self._transport.peers.values())
        ]

    def send_to(self, node_id: str, msg_type: int, payload: dict[str, Any]) -> None:
        if self._loop is None or self._transport is None:
            return
        peer = self._transport.peers.get(node_id)
        if peer is None:
            return
        asyncio.run_coroutine_threadsafe(
            self._transport.send(peer, msg_type, payload), self._loop
        )

    async def send_in_loop(
        self, peer: Peer, msg_type: int, payload: dict[str, Any]
    ) -> None:
        if self._transport is None:
            raise TransportError("transport is not running")
        await self._transport.send(peer, msg_type, payload)

    def broadcast(self, msg_type: int, payload: dict[str, Any], tags: list[str]) -> None:
        if self._loop is None or self._transport is None:
            return
        for peer in list(self._transport.peers.values()):
            if tag_match(tags, peer.tags):
                asyncio.run_coroutine_threadsafe(
                    self._transport.send(peer, msg_type, payload), self._loop
                )

    def stop(self) -> None:
        if self._loop is not None and self._transport is not None and self._loop.is_running():
            future = asyncio.run_coroutine_threadsafe(self._transport.stop(), self._loop)
            try:
                future.result(timeout=5)
            except Exception:
                pass
        if self._loop is not None and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread.is_alive():
            self._thread.join(timeout=5)

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            host, port = parse_address(self.config.listen)
            self._transport = Transport(
                token=self.config.token.get_secret_value(),
                node_id=self.node_id,
                tags=self.config.tags,
                role=self.config.role,
                on_message=self.on_message,
            )
            loop.run_until_complete(self._transport.start(host, port))
            self._ready.set()
            loop.run_forever()
        except Exception as exc:
            self._error = exc
            self._ready.set()
        finally:
            if self._transport is not None:
                try:
                    loop.run_until_complete(self._transport.stop())
                except Exception:
                    pass
            loop.close()
