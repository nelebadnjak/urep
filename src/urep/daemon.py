from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import stat
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .config import Config, make_node_id
from .discovery import Discovery, DiscoveryError
from .fsops import atomic_write, ensure_parent
from .gitrepo import Change, FileState, GitRepo, blob_hash, message_id
from .hlc import HLC, Clock
from .hooks import normalize_commands, run_hooks
from .paths import (
    is_forbidden,
    is_under,
    mirror_rel,
    normalize,
    real_parent_allowed,
    tag_match,
)
from .protocol import Msg
from .state import EventLog, RuntimeState
from .transport import Peer, TransportError, TransportService
from .watcher import FileWatcher

log = logging.getLogger("urep.daemon")

MAX_INLINE = 900 * 1024
CHUNK_SIZE = 256 * 1024
MAX_CHUNKS = 4096


def _batches(items: list[Any], size: int) -> list[list[Any]]:
    return [items[index : index + size] for index in range(0, len(items), size)]


class Daemon:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.node_id = make_node_id(config.prefix)
        self.clock = Clock(self.node_id)
        self.repo = GitRepo(config.repo_dir)
        self.repo.ensure()
        self.state = self.repo.derive_state()
        for item in self.state.values():
            self.clock.observe(item.version)
        self.runtime = RuntimeState(config.state_dir / "state.json")
        self.events = EventLog(config.state_dir / "events.jsonl")
        self.watcher: FileWatcher | None = None
        self.transport: TransportService | None = None
        self.discovery: Discovery | None = None

        self._apply_lock = threading.RLock()
        self._connecting: set[str] = set()
        self._connecting_lock = threading.Lock()
        self._last_manifest: dict[str, float] = {}
        self._sync_lock = threading.Lock()
        self._pending_chunks: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------ run

    def run(self) -> None:
        logging.basicConfig(
            level=getattr(logging, self.config.log_level.upper(), logging.INFO),
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )
        log.info(
            "starting node=%s role=%s tags=%s paths=%s",
            self.node_id,
            self.config.role,
            ",".join(self.config.tags) or "-",
            ",".join(str(path) for path in self.config.paths) or "-",
        )
        log.info(
            "repo=%s commits=%d state=%d",
            self.config.repo_dir,
            self.repo.commit_count(),
            len(self.state),
        )

        stop = threading.Event()

        try:
            self.transport = TransportService(self.config, self.node_id, self._on_message)
            self.transport.start()
            log.info("transport listening on %s", self.config.listen)
        except TransportError as exc:
            log.error("transport failed: %s", exc)
            self.transport = None

        try:
            self.discovery = Discovery(
                self.config,
                self.node_id,
                self._on_peer,
                peers_path=self.config.state_dir / "peers.json",
            )
            self.discovery.start()
        except DiscoveryError as exc:
            log.error("discovery failed: %s", exc)
            self.discovery = None

        for seed in self.config.seeds:
            threading.Thread(
                target=self._connect_address, args=(seed,), daemon=True
            ).start()

        threading.Thread(
            target=self._replication_loop, args=(stop,), daemon=True
        ).start()

        if self.config.role == "peer":
            for path in self.config.paths:
                ensure_parent(path, self.config.dir_mode)
            if self.config.paths:
                self.watcher = FileWatcher(
                    self.config.paths,
                    self.config.debounce_ms,
                    self._handle_batch,
                    self.config.dir_mode,
                )
                self.watcher.start()
                if not self.watcher.wait_ready(5):
                    log.warning("watcher did not become ready in time")

        def _stop(signum: int, frame: object) -> None:
            stop.set()

        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)

        while not stop.wait(1.0):
            pass

        if self.watcher is not None:
            self.watcher.stop()
        if self.discovery is not None:
            self.discovery.stop()
        if self.transport is not None:
            self.transport.stop()
        log.info("stopped node=%s", self.node_id)

    def _replication_loop(self, stop: threading.Event) -> None:
        while not stop.wait(1.0):
            transport = self.transport
            if transport is None:
                continue
            peers = {item["node_id"] for item in transport.peers_snapshot()}
            self._write_peers()
            now = time.time()
            with self._sync_lock:
                for node_id in list(self._last_manifest):
                    if node_id not in peers:
                        del self._last_manifest[node_id]
            for node_id in peers:
                with self._sync_lock:
                    last = self._last_manifest.get(node_id, 0.0)
                if now - last < self.config.anti_entropy_s:
                    continue
                if self.runtime.paused("out"):
                    continue
                try:
                    self._send_manifest(node_id)
                    with self._sync_lock:
                        self._last_manifest[node_id] = now
                except Exception:
                    log.exception("manifest send to %s failed", node_id)

    def _send_manifest(self, node_id: str) -> None:
        with self._apply_lock:
            entries = [
                {
                    "p": f"/{item.path}",
                    "v": str(item.version),
                    "h": item.blob,
                    "m": f"{item.mode:04o}",
                    "d": item.deleted,
                    "tags": self.config.tags,
                    "hp": item.pre_hooks,
                    "ho": item.post_hooks,
                }
                for item in self.state.values()
            ]
        if self.transport is None:
            return
        for batch in _batches(entries, 200) or [[]]:
            self.transport.send_to(node_id, Msg.MANIFEST, {"entries": batch})

    # ------------------------------------------------------------ discovery

    def _on_peer(self, entry: dict) -> None:
        node_id = entry.get("node_id")
        address = entry.get("address")
        if not node_id or not address or node_id == self.node_id:
            return
        if self.transport is None or self.transport.has_peer(node_id):
            return
        with self._connecting_lock:
            if node_id in self._connecting:
                return
            self._connecting.add(node_id)
        threading.Thread(
            target=self._connect_discovered,
            args=(node_id, address),
            daemon=True,
        ).start()

    def _connect_discovered(self, node_id: str, address: str) -> None:
        try:
            if self.transport is None:
                return
            self.transport.connect(address)
            log.info("connected to %s at %s", node_id, address)
        except TransportError as exc:
            log.debug("connect to %s failed: %s", address, exc)
        finally:
            with self._connecting_lock:
                self._connecting.discard(node_id)

    def _connect_address(self, address: str) -> None:
        if self.transport is None:
            return
        try:
            self.transport.connect(address)
            log.info("connected to seed %s", address)
        except TransportError as exc:
            log.warning("seed %s failed: %s", address, exc)

    def _write_peers(self) -> None:
        entries: dict[str, dict[str, Any]] = {}
        if self.discovery is not None:
            for item in self.discovery.snapshot():
                entries[str(item["node_id"])] = dict(item)
        if self.transport is not None:
            now = time.time()
            for item in self.transport.peers_snapshot():
                node_id = str(item["node_id"])
                entry = entries.get(node_id, {})
                entry.update(item)
                entry["last_seen"] = now
                entry["connected"] = True
                entries[node_id] = entry
        data = {
            "updated_at": time.time(),
            "peers": sorted(entries.values(), key=lambda item: str(item["node_id"])),
        }
        path = self.config.state_dir / "peers.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(
            f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        tmp.write_text(
            json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        os.replace(tmp, path)

    # --------------------------------------------------------------- hooks

    def _hooks(self) -> tuple[list[list[str]], list[list[str]]]:
        if not self.config.hooks_enabled:
            return [], []
        pre = [["sh", "-c", command] for command in self.config.pre_hooks]
        post = [["sh", "-c", command] for command in self.config.post_hooks]
        return pre, post

    # ------------------------------------------------------------ local fs

    def _handle_batch(self, paths: list[Path]) -> None:
        for path in paths:
            try:
                with self._apply_lock:
                    self._handle_one(path)
            except Exception:
                log.exception("failed to process %s", path)
                self.events.write(
                    level="error",
                    node=self.node_id,
                    action="watch",
                    path=str(path),
                    result="error",
                )

    def _handle_one(self, path: Path) -> None:
        rel = mirror_rel(path)
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            current = self.state.get(rel)
            if current is not None and not current.deleted:
                self._commit_delete(path, rel, current)
            return

        if stat.S_ISLNK(info.st_mode):
            self.events.write(
                level="warning",
                node=self.node_id,
                action="skip",
                path=str(path),
                result="symlink",
            )
            return
        if not stat.S_ISREG(info.st_mode):
            self.events.write(
                level="warning",
                node=self.node_id,
                action="skip",
                path=str(path),
                result="not_a_file",
            )
            return

        data = path.read_bytes()
        if len(data) > self.config.max_file_size:
            self.events.write(
                level="warning",
                node=self.node_id,
                action="skip",
                path=str(path),
                result="too_large",
            )
            return

        blob = blob_hash(data)
        mode = info.st_mode & 0o7777
        current = self.state.get(rel)

        if (
            current is not None
            and not current.deleted
            and current.blob == blob
            and current.mode == mode
        ):
            return

        op = "A" if current is None or current.deleted else "M"
        force = current is not None and not current.deleted and current.blob == blob
        self.repo.write_mirror(rel, data, mode)
        version = self.clock.tick()
        pre, post = self._hooks()
        change = Change(rel, op, blob, mode, version, pre, post)
        if self._commit(path, rel, change, force=force, data=data):
            log.info("committed %s (%s %s)", path, op, version)

    def _commit_delete(self, path: Path, rel: str, current: FileState) -> None:
        self.repo.remove_mirror(rel)
        version = self.clock.tick()
        pre, post = self._hooks()
        change = Change(rel, "D", current.blob, current.mode, version, pre, post)
        if self._commit(path, rel, change, force=False, data=None):
            log.info("committed delete %s (%s)", path, version)

    def _commit(
        self,
        path: Path,
        rel: str,
        change: Change,
        *,
        force: bool,
        data: bytes | None,
    ) -> bool:
        msg = message_id("local", rel, change.version, change.blob)
        sha = self.repo.commit(
            node=self.node_id,
            msg_id=msg,
            changes=[change],
            timestamp_ms=int(time.time() * 1000),
            force=force,
        )
        if sha is None:
            return False
        self.state[rel] = FileState(
            path=rel,
            blob=change.blob,
            version=change.version,
            mode=change.mode,
            deleted=change.op == "D",
            pre_hooks=change.pre_hooks,
            post_hooks=change.post_hooks,
            commit=sha,
            ts=int(time.time()),
        )
        self.events.write(
            level="info",
            node=self.node_id,
            action="delete" if change.op == "D" else "update",
            path=str(path),
            peer=None,
            result="ok",
            version=str(change.version),
            commit=sha,
        )
        self._status("GREEN", path)
        self._broadcast_change(change, data)
        return True

    # ------------------------------------------------------------- network

    async def _on_message(
        self, peer: Peer, msg_type: int, payload: dict[str, Any]
    ) -> None:
        try:
            message = Msg(msg_type)
        except ValueError:
            log.debug("unknown message type %s from %s", msg_type, peer.node_id)
            return
        if message == Msg.MANIFEST:
            if self.runtime.paused("in"):
                return
            self._handle_manifest(peer, payload)
        elif message == Msg.FILE_GET:
            if self.runtime.paused("out"):
                return
            self._handle_file_get(peer, payload)
        elif message == Msg.FILE_PUT:
            await self._handle_file_put(peer, payload)
        elif message == Msg.FILE_CHUNK:
            if self.runtime.paused("in"):
                return
            await self._handle_file_chunk(peer, payload)
        elif message == Msg.FILE_DELETE:
            await self._handle_file_delete(peer, payload)
        elif message in (Msg.ACK, Msg.NACK):
            log.info(
                "peer=%s %s path=%s result=%s",
                peer.node_id,
                message.name,
                payload.get("path"),
                payload.get("result"),
            )
        else:
            log.debug("message %s from %s", message.name, peer.node_id)

    def _handle_manifest(self, peer: Peer, payload: dict[str, Any]) -> None:
        entries = payload.get("entries")
        if not isinstance(entries, list):
            return
        requests: list[dict[str, Any]] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            path_raw = entry.get("p")
            version_raw = entry.get("v")
            if not isinstance(path_raw, str) or not isinstance(version_raw, str):
                continue
            try:
                version = HLC.parse(version_raw)
                path = normalize(path_raw)
            except Exception:
                continue
            rel = mirror_rel(path)
            with self._apply_lock:
                local = self.state.get(rel)
                if local is not None and version <= local.version:
                    continue
            requests.append(
                {
                    "path": path_raw,
                    "version": version_raw,
                    "blob": entry.get("h"),
                }
            )
        if requests and self.transport is not None:
            for batch in _batches(requests, 200):
                self.transport.send_to(peer.node_id, Msg.FILE_GET, {"entries": batch})

    def _handle_file_get(self, peer: Peer, payload: dict[str, Any]) -> None:
        entries = payload.get("entries")
        if not isinstance(entries, list) or self.transport is None:
            return
        with self._apply_lock:
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                path_raw = entry.get("path")
                if not isinstance(path_raw, str):
                    continue
                try:
                    rel = mirror_rel(normalize(path_raw))
                except Exception:
                    continue
                local = self.state.get(rel)
                if local is None:
                    continue
                base = {
                    "path": path_raw,
                    "version": str(local.version),
                    "blob": local.blob,
                    "mode": f"{local.mode:04o}",
                    "tags": self.config.tags,
                    "hp": local.pre_hooks,
                    "ho": local.post_hooks,
                    "node_id": self.node_id,
                    "msg_id": message_id(
                        "serve", rel, local.version, local.blob
                    ),
                }
                if local.deleted:
                    self.transport.send_to(peer.node_id, Msg.FILE_DELETE, base)
                    continue
                try:
                    data = (self.repo.repo_dir / rel).read_bytes()
                except OSError:
                    continue
                if blob_hash(data) != local.blob:
                    continue
                self._send_file_payload(peer.node_id, base, data)

    async def _handle_file_put(
        self, peer: Peer, payload: dict[str, Any]
    ) -> None:
        data = payload.get("data")
        if data is None:
            transfer = payload.get("transfer")
            count = payload.get("chunks")
            if (
                not isinstance(transfer, str)
                or not isinstance(count, int)
                or not 1 <= count <= MAX_CHUNKS
            ):
                return
            meta = {key: value for key, value in payload.items() if key != "data"}
            self._pending_chunks[transfer] = {
                "meta": meta,
                "chunks": [],
                "received": 0,
                "count": count,
            }
            return
        if not isinstance(data, bytes):
            return
        result = await asyncio.to_thread(self._apply_remote, payload, peer.node_id)
        await self._send_result(peer, payload, result)

    async def _handle_file_chunk(
        self, peer: Peer, payload: dict[str, Any]
    ) -> None:
        transfer = payload.get("transfer")
        seq = payload.get("seq")
        data = payload.get("data")
        if (
            not isinstance(transfer, str)
            or not isinstance(seq, int)
            or not isinstance(data, bytes)
        ):
            return
        pending = self._pending_chunks.get(transfer)
        if pending is None or seq != pending["received"]:
            return
        pending["chunks"].append(data)
        pending["received"] += 1
        if pending["received"] < pending["count"]:
            return
        meta = pending["meta"]
        del self._pending_chunks[transfer]
        meta["data"] = b"".join(pending["chunks"])
        result = await asyncio.to_thread(self._apply_remote, meta, peer.node_id)
        await self._send_result(peer, meta, result)

    async def _handle_file_delete(
        self, peer: Peer, payload: dict[str, Any]
    ) -> None:
        request = dict(payload)
        request["deleted"] = True
        result = await asyncio.to_thread(self._apply_remote, request, peer.node_id)
        await self._send_result(peer, request, result)

    async def _send_result(
        self, peer: Peer, payload: dict[str, Any], result: tuple[bool, str]
    ) -> None:
        if self.transport is None:
            return
        ok, reason = result
        message = Msg.ACK if ok else Msg.NACK
        await self.transport.send_in_loop(
            peer, message, {"path": payload.get("path"), "result": reason}
        )

    def _send_file_payload(
        self, node_id: str, payload: dict[str, Any], data: bytes
    ) -> None:
        if self.transport is None:
            return
        if len(data) <= MAX_INLINE:
            message = dict(payload)
            message["data"] = data
            self.transport.send_to(node_id, Msg.FILE_PUT, message)
            return
        chunks = (len(data) + CHUNK_SIZE - 1) // CHUNK_SIZE
        if chunks > MAX_CHUNKS:
            log.warning("file too large to transfer: %s chunks", chunks)
            return
        transfer = uuid.uuid4().hex
        header = dict(payload)
        header["data"] = None
        header["transfer"] = transfer
        header["chunks"] = chunks
        self.transport.send_to(node_id, Msg.FILE_PUT, header)
        for seq in range(chunks):
            chunk = data[seq * CHUNK_SIZE : (seq + 1) * CHUNK_SIZE]
            self.transport.send_to(
                node_id,
                Msg.FILE_CHUNK,
                {"transfer": transfer, "seq": seq, "data": chunk},
            )

    def _broadcast_file(
        self,
        payload: dict[str, Any],
        data: bytes,
        *,
        tags: list[str] | None = None,
        exclude: str | None = None,
    ) -> None:
        if self.transport is None:
            return
        channel = tags or self.config.tags
        for peer in self.transport.peers_snapshot():
            if exclude is not None and peer["node_id"] == exclude:
                continue
            if tag_match(channel, peer["tags"]):
                self._send_file_payload(peer["node_id"], payload, data)

    def _broadcast_change(
        self,
        change: Change,
        data: bytes | None,
        *,
        tags: list[str] | None = None,
        exclude: str | None = None,
    ) -> None:
        if self.transport is None:
            return
        if self.runtime.paused("out"):
            return
        channel = tags or self.config.tags
        payload = {
            "path": f"/{change.path}",
            "version": str(change.version),
            "blob": change.blob,
            "mode": f"{change.mode:04o}",
            "tags": channel,
            "hp": change.pre_hooks,
            "ho": change.post_hooks,
            "node_id": self.node_id,
            "msg_id": message_id(
                "local", change.path, change.version, change.blob
            ),
        }
        if change.op == "D":
            for peer in self.transport.peers_snapshot():
                if exclude is not None and peer["node_id"] == exclude:
                    continue
                if tag_match(channel, peer["tags"]):
                    self.transport.send_to(
                        peer["node_id"], Msg.FILE_DELETE, payload
                    )
        else:
            self._broadcast_file(
                payload, data or b"", tags=channel, exclude=exclude
            )

    # --------------------------------------------------------------- apply

    def _apply_remote(
        self, payload: dict[str, Any], peer_node: str
    ) -> tuple[bool, str]:
        try:
            with self._apply_lock:
                return self._apply_remote_locked(payload, peer_node)
        except Exception:
            log.exception("remote apply failed")
            self.events.write(
                level="error",
                node=self.node_id,
                action="apply",
                path=str(payload.get("path")),
                peer=peer_node,
                result="error",
            )
            return False, "error"

    def _apply_remote_locked(
        self, payload: dict[str, Any], peer_node: str
    ) -> tuple[bool, str]:
        if self.runtime.paused("in"):
            return False, "paused"
        path_raw = payload.get("path")
        if not isinstance(path_raw, str):
            return False, "bad_path"
        try:
            path = normalize(path_raw)
        except Exception:
            return False, "bad_path"
        if is_forbidden(path, [self.config.repo_dir, self.config.state_dir]):
            return False, "path_forbidden"
        if not is_under(path, self.config.accept_roots):
            return False, "path_not_allowed"
        if not real_parent_allowed(path, self.config.accept_roots):
            return False, "path_not_allowed"

        tags = payload.get("tags")
        if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
            return False, "bad_tags"
        if not tag_match(tags, self.config.tags):
            return False, "tag_mismatch"

        version_raw = payload.get("version")
        if not isinstance(version_raw, str):
            return False, "bad_version"
        try:
            version = HLC.parse(version_raw)
        except Exception:
            return False, "bad_version"

        try:
            mode = int(str(payload.get("mode", "0644")), 8)
        except ValueError:
            return False, "bad_mode"

        deleted = bool(payload.get("deleted"))
        rel = mirror_rel(path)
        local = self.state.get(rel)
        if local is not None and version <= local.version:
            return True, "noop"

        if local is None and path.exists() and not path.is_symlink():
            self._snapshot_local(path)

        if deleted and local is None and not path.exists():
            return True, "noop"

        pre = normalize_commands(payload.get("hp"))
        post = normalize_commands(payload.get("ho"))
        if not self.config.hooks_enabled:
            pre, post = [], []
        action = "delete" if deleted else "update"
        env = {
            "UREP_PATH": str(path),
            "UREP_ACTION": action,
            "UREP_PEER": peer_node,
            "UREP_VERSION": str(version),
            "UREP_MODE": f"{mode:04o}",
        }
        cwd = path.parent if path.parent.exists() else self.config.state_dir
        pre_result = run_hooks(
            pre, env=env, timeout_s=self.config.hook_timeout_s, cwd=cwd
        )
        if not pre_result.ok:
            self.events.write(
                level="error",
                node=self.node_id,
                action=action,
                path=str(path),
                peer=peer_node,
                result="pre_hook_failed",
                hook_exit=pre_result.code,
                hook_output=pre_result.output,
            )
            self._status("RED", path)
            return False, "pre_hook_failed"

        content: bytes | None = None
        if deleted:
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                log.error("cannot delete %s: %s", path, exc)
                return False, "write_failed"
            self.repo.remove_mirror(rel)
            blob = local.blob if local is not None else payload.get("blob")
            op = "D"
            force = False
            atomic_ok = True
        else:
            data = payload.get("data")
            if not isinstance(data, bytes):
                return False, "bad_data"
            if len(data) > self.config.max_file_size:
                return False, "too_large"
            content = data
            blob = blob_hash(data)
            expected = payload.get("blob")
            if isinstance(expected, str) and expected != blob:
                return False, "bad_hash"
            ensure_parent(path, self.config.dir_mode)
            atomic_ok = atomic_write(path, data, mode)
            self.repo.write_mirror(rel, data, mode)
            op = "A" if local is None or local.deleted else "M"
            force = (
                local is not None
                and not local.deleted
                and local.blob == blob
                and local.mode != mode
            )

        change = Change(rel, op, blob, mode, version, pre, post)
        msg = payload.get("msg_id")
        if not isinstance(msg, str) or not msg:
            msg = message_id("remote", rel, version, blob)
        sha = self.repo.commit(
            node=peer_node,
            msg_id=msg,
            changes=[change],
            timestamp_ms=int(time.time() * 1000),
            force=force,
        )
        if sha is None:
            return True, "noop"

        self.state[rel] = FileState(
            path=rel,
            blob=blob,
            version=version,
            mode=mode,
            deleted=deleted,
            pre_hooks=pre,
            post_hooks=post,
            commit=sha,
            ts=int(time.time()),
        )
        self.clock.observe(version)
        self._broadcast_change(
            change, content, tags=[str(tag) for tag in tags], exclude=peer_node
        )
        color = "GREEN"
        if not atomic_ok:
            color = "YELLOW"
            self.events.write(
                level="warning",
                node=self.node_id,
                action=action,
                path=str(path),
                peer=peer_node,
                result="non_atomic_write",
            )
        post_result = run_hooks(
            post, env=env, timeout_s=self.config.hook_timeout_s, cwd=cwd
        )
        if not post_result.ok:
            color = "YELLOW"
            self.events.write(
                level="warning",
                node=self.node_id,
                action=action,
                path=str(path),
                peer=peer_node,
                result="hook_failed",
                hook_exit=post_result.code,
                hook_output=post_result.output,
            )
            self._status(color, path)
            return True, "hook_failed"
        self.events.write(
            level="info",
            node=self.node_id,
            action=action,
            path=str(path),
            peer=peer_node,
            result="ok",
            version=str(version),
            commit=sha,
        )
        self._status(color, path)
        return True, "ok"

    def _snapshot_local(self, path: Path) -> None:
        rel = mirror_rel(path)
        try:
            info = os.lstat(path)
        except OSError:
            return
        if not stat.S_ISREG(info.st_mode):
            return
        data = path.read_bytes()
        if len(data) > self.config.max_file_size:
            return
        blob = blob_hash(data)
        mode = info.st_mode & 0o7777
        self.repo.write_mirror(rel, data, mode)
        version = self.clock.tick()
        change = Change(rel, "A", blob, mode, version)
        sha = self.repo.commit(
            node=self.node_id,
            msg_id=message_id("snapshot", rel, version, blob),
            changes=[change],
            timestamp_ms=int(time.time() * 1000),
        )
        if sha is None:
            return
        self.state[rel] = FileState(
            path=rel,
            blob=blob,
            version=version,
            mode=mode,
            deleted=False,
            pre_hooks=[],
            post_hooks=[],
            commit=sha,
            ts=int(time.time()),
        )
        self.events.write(
            level="info",
            node=self.node_id,
            action="snapshot",
            path=str(path),
            peer=None,
            result="ok",
            version=str(version),
            commit=sha,
        )

    def _status(self, color: str, path: Path) -> None:
        if self.discovery is not None:
            self.discovery.update_status(color, str(path))
