from __future__ import annotations

import ipaddress
import json
import logging
import os
import socket
import struct
import threading
import time
from pathlib import Path
from typing import Any

import cbor2

from .config import Config
from .crypto import CryptoError, decrypt_discovery, encrypt_discovery
from .protocol import PROTO_VERSION

log = logging.getLogger("urep.discovery")

MAX_PACKET = 64 * 1024
MAX_CLOCK_SKEW = 120


class DiscoveryError(RuntimeError):
    pass


def parse_group(value: str) -> tuple[str, int]:
    host, sep, port = value.rpartition(":")
    if not sep or not host or not port.isdigit():
        raise DiscoveryError(f"invalid multicast group: {value!r}")
    number = int(port)
    if not 1 <= number <= 65535:
        raise DiscoveryError(f"invalid multicast port: {value!r}")
    return host, number


def is_multicast(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_multicast
    except ValueError:
        return False


class Discovery:
    """Encrypted UDP multicast announcements plus gossip."""

    def __init__(
        self,
        config: Config,
        node_id: str,
        on_peer: Any = None,
        peers_path: Path | None = None,
    ) -> None:
        self.config = config
        self.node_id = node_id
        self.on_peer = on_peer
        self.peers_path = peers_path
        self.peers: dict[str, dict[str, Any]] = {}
        self.color = "GREEN"
        self.last_path: str | None = None

        self._group = parse_group(config.multicast_group)
        self._tcp_port = parse_group(config.listen)[1]
        self._sock: socket.socket | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def start(self) -> None:
        if not self.config.multicast:
            log.info("multicast disabled")
            return
        host, port = self._group
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("", port))
        except OSError as exc:
            sock.close()
            raise DiscoveryError(f"cannot bind UDP {port}: {exc}") from exc
        if is_multicast(host):
            membership = struct.pack(
                "4s4s", socket.inet_aton(host), socket.inet_aton("0.0.0.0")
            )
            try:
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)
            except OSError as exc:
                log.warning("cannot join multicast %s: %s", host, exc)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
        sock.settimeout(0.5)
        self._sock = sock
        self._thread = threading.Thread(target=self._run, name="urep-discovery", daemon=True)
        self._thread.start()
        log.info("discovery on %s:%d as %s", host, port, self.node_id)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)
        if self._sock is not None:
            self._sock.close()
            self._sock = None

    def update_status(self, color: str, last_path: str | None) -> None:
        self.color = color
        self.last_path = last_path

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(item) for item in self.peers.values()]

    def ingest(self, packet: bytes, address: tuple[str, int]) -> None:
        self._handle_packet(packet, address)

    def _run(self) -> None:
        assert self._sock is not None
        last_announce = 0.0
        while not self._stop.is_set():
            now = time.time()
            if now - last_announce >= self.config.announce_interval_s:
                self._announce(now)
                last_announce = now
            try:
                packet, address = self._sock.recvfrom(MAX_PACKET)
            except TimeoutError:
                packet = b""
                address = ("", 0)
            except OSError:
                if self._stop.is_set():
                    return
                continue
            if packet:
                self._handle_packet(packet, address)
            self._prune(now)

    def _announce(self, now: float | None = None) -> None:
        if self._sock is None:
            return
        stamp = time.time() if now is None else now
        with self._lock:
            gossip = [
                {
                    "node_id": item["node_id"],
                    "tags": item["tags"],
                    "role": item["role"],
                    "address": item["address"],
                    "tcp_port": item.get("tcp_port"),
                    "seen": max(0, int(stamp - item["last_seen"])),
                }
                for item in self.peers.values()
            ][:32]
        payload = {
            "proto": PROTO_VERSION,
            "node_id": self.node_id,
            "tags": self.config.tags,
            "role": self.config.role,
            "tcp_port": self._tcp_port,
            "ts": stamp,
            "color": self.color,
            "last_path": self.last_path,
            "peers": gossip,
        }
        packet = encrypt_discovery(
            self.config.token.get_secret_value(), cbor2.dumps(payload)
        )
        host, port = self._group
        if len(packet) > MAX_PACKET:
            log.warning("announce packet too large, skipping")
            return
        try:
            self._sock.sendto(packet, (host, port))
        except OSError as exc:
            log.debug("announce failed: %s", exc)

    def _handle_packet(self, packet: bytes, address: tuple[str, int]) -> None:
        try:
            payload = cbor2.loads(
                decrypt_discovery(self.config.token.get_secret_value(), packet)
            )
        except (CryptoError, ValueError, cbor2.CBORDecodeError):
            return
        if not isinstance(payload, dict) or payload.get("proto") != PROTO_VERSION:
            return
        node_id = payload.get("node_id")
        if not isinstance(node_id, str) or not node_id or node_id == self.node_id:
            return
        try:
            stamp = float(payload.get("ts", 0))
        except (TypeError, ValueError):
            return
        if abs(time.time() - stamp) > MAX_CLOCK_SKEW:
            return
        tcp_port = payload.get("tcp_port")
        if not isinstance(tcp_port, int) or not 1 <= tcp_port <= 65535:
            return
        tags = payload.get("tags")
        role = payload.get("role")
        if not isinstance(tags, list) or role not in {"peer", "slave"}:
            return
        now = time.time()
        entry = {
            "node_id": node_id,
            "tags": [str(tag) for tag in tags],
            "role": str(role),
            "address": f"{address[0]}:{tcp_port}",
            "tcp_port": tcp_port,
            "last_seen": now,
            "color": str(payload.get("color", "GREEN")),
            "last_path": payload.get("last_path"),
        }
        new_peers: list[dict[str, Any]] = []
        new_peer = False
        with self._lock:
            previous = self.peers.get(node_id)
            if previous is None or previous.get("address") != entry["address"]:
                new_peer = True
            self.peers[node_id] = entry
            gossip = payload.get("peers")
            if not isinstance(gossip, list):
                gossip = []
            for item in gossip:
                if not isinstance(item, dict):
                    continue
                other = item.get("node_id")
                other_port = item.get("tcp_port")
                if not isinstance(other, str) or other == self.node_id:
                    continue
                if other in self.peers:
                    continue
                seen = item.get("seen", MAX_CLOCK_SKEW)
                if not isinstance(seen, int) or seen > self.config.peer_timeout_s:
                    continue
                gossip_address = self._gossip_address(item, address[0], other_port)
                if not gossip_address:
                    continue
                gossip_entry = {
                    "node_id": other,
                    "tags": [str(tag) for tag in item.get("tags", [])],
                    "role": str(item.get("role", "peer")),
                    "address": gossip_address,
                    "tcp_port": other_port if isinstance(other_port, int) else None,
                    "last_seen": now - seen,
                    "color": str(item.get("color", "GREEN")),
                    "last_path": None,
                }
                self.peers[other] = gossip_entry
                new_peers.append(gossip_entry)
        self._write_peers()
        if new_peer:
            new_peers.insert(0, entry)
        if self.on_peer is not None:
            for discovered in new_peers:
                try:
                    self.on_peer(discovered)
                except Exception:
                    log.exception("peer callback failed")

    @staticmethod
    def _gossip_address(
        item: dict[str, Any], source_ip: str, other_port: Any
    ) -> str | None:
        address = item.get("address")
        if isinstance(address, str):
            return address
        port = item.get("tcp_port", other_port)
        if isinstance(port, int) and 1 <= port <= 65535:
            return f"{source_ip}:{port}"
        return None

    def _prune(self, now: float) -> None:
        cutoff = now - self.config.peer_timeout_s
        removed = False
        with self._lock:
            expired = [
                key for key, value in self.peers.items() if value["last_seen"] < cutoff
            ]
            for node_id in expired:
                del self.peers[node_id]
                removed = True
        if removed:
            self._write_peers()

    def _write_peers(self) -> None:
        if self.peers_path is None:
            return
        with self._lock:
            peers = sorted(
                (dict(item) for item in self.peers.values()),
                key=lambda item: item["node_id"],
            )
        data = {"updated_at": time.time(), "peers": peers}
        self.peers_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.peers_path.with_suffix(self.peers_path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, self.peers_path)
