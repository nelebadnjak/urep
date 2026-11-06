# uRep — specification v1.8

**micro replication** — a decentralized P2P sync agent for configuration files. No leader, no database, with a local git repository as the transactional history.

Status: draft approved for development
Date: 2026-11-04
Implementation language: Python 3.11+

---

## 0. Decisions

| Topic | Decision |
|---|---|
| Topology | P2P mesh, no leader, LAN / same subnet |
| Discovery | UDP multicast + gossip (seed list optional) |
| Auth | one cluster token → HKDF → ChaCha20-Poly1305, no PKI |
| Targeting | tags as a subscription: a node listens to messages with its tags |
| Consistency | eventual + HLC LWW, conflicts are marked `YELLOW` |
| State | **git is the only source**; state is derived from history at startup, no index |
| Node ID | disposable UUID at startup, not persisted; `--prefix` for readability only |
| Git | system `git` (subprocess), SHA-256 repo, history kept forever |
| Paths | only explicitly listed individual files; metadata = content + mode |
| Symlink | out of v1 — skip + warning |
| Hooks | travel with the file, executed only by the receiver, on update/catch-up/delete |
| Deletion | tombstone (git delete commit), hooks are executed |
| Mode | `peer` (sends + receives) or `slave` (receives only, may pull) |
| Configuration | **CLI + ENV only; no config file** |
| Deployment | container-first: Docker volume + ENV |
| Status | node-level color + last action |
| Control | `urep pause` / `urep resume` |

## 1. Goals / non-goals

**Goals:** G1 P2P without a leader · G2 autodiscovery · G3 token auth+encryption · G4 tag subscription · G5 watch→push · G6 hash per message · G7 pre/post hook · G8 eventual consistency + LWW · G9 CLI+daemon · G10 git history · G11 pause/resume · G12 `--slave` mode · G13 container without a config file · G14 zero extra state (git only).

**Non-goals (v1):** GUI · binary files >16 MiB · multi-writer merge (CRDT) · NAT traversal/relay · key rotation · Windows · uid/gid/xattr/ACL/SELinux · symlinks · recursive directories · directory auto-scan · empty directories · **config files (TOML/YAML/JSON)** · per-path tags · per-path hooks · runtime configuration changes.

## 2. Architecture

```
┌──────────────────────────── uRep daemon (one per node) ────────────────────────────────────┐
│                                                                                            │
│ watcher ──► commit ──► replicator ──► transport (TCP+AEAD) ◄──► discovery (UDP multicast) │
│ (peer only)    │            │  ▲              │                        + gossip             │
│                ▼            ▼  │              ▼                                            │
│         repo/ (git) ◄──── apply ──► write ──► hooks ──► status/events                      │
└────────────────────────────────────────────────────────────────────────────────────────────┘
                ▲
        urep CLI ── unix socket ── /var/lib/urep/run/urep.sock
```

- **No coordinator**, no quorum, no leader election.
- **No durable message queue.** After a partition, state is reconciled through an anti-entropy manifest diff.
- **No database, no index, no config file.** `state.json` only holds `node_id`/`paused`; `events.jsonl` is the log.
- **Everything is idempotent**: the same message is rejected because the version is not newer, or because the content is the same.

## 3. On-disk layout

```
/var/lib/urep/                     # UREP_STATE_DIR
├── repo/                          # git mirror (0700, SHA-256 repo)
│   ├── .git/
│   └── urep/app.yaml              # mirror for /urep/app.yaml
├── events.jsonl                   # append-only, rotation
├── state.json                     # paused (disposable)
└── run/urep.sock

/etc/urep/token                    # optional: token file (0600)
/urep/                             # UREP_DATA_DIR (container data volume)
```

Git **never** gets `--work-tree=/`. Real files are written exclusively by our atomic write (with the container fallback, 4.5).

**Node ID (disposable):**

- Always `uuid4()`, generated at every startup; **never persisted**.
- `UREP_PREFIX` / `--prefix` → `<prefix>-<uuid4()>` (e.g. `bg-3f9a1c2b-...`), for readability only (site/hostname part).
- No user-supplied name; identity cannot be pinned.
- `urep id [--prefix P]` only prints the ID that would be generated (informational).
- After a restart the node has a new ID; peers see it as a new node, and the old record expires after `peer_timeout` (30 s).
- UUID4 collision is practically impossible; there are no user-supplied names, so there are no duplicates.
- The node ID enters the HLC tiebreak and the `Urep-Node` commit trailer.

**Forbidden paths (hard deny):** `/etc/urep`, `/var/lib/urep`, `/proc`, `/sys`, `/dev`, `/run`, the repo directory, and anything that contains `..` after normalization.

## 4. Configuration

**No config file.** Everything comes from CLI flags or `UREP_*` env variables. Order: **CLI > ENV > built-in default.** Lists are replaced, not merged.

### 4.1 ENV table

| ENV | Default | CLI |
|---|---|---|
| `UREP_TOKEN` | — (required if no file) | `--token` |
| `UREP_TOKEN_FILE` | — | `--token-file` |
| `UREP_PREFIX` | — | `--prefix` |
| `UREP_TAG` (comma-separated) | `default` | `--tag` (repeatable) |
| `UREP_ROLE` (`peer`/`slave`) | `peer` | `--slave` |
| `UREP_PATH` (comma-separated) | empty → receive-only | `--path` (repeatable) |
| `UREP_DATA_DIR` | `/urep` | — |
| `UREP_STATE_DIR` | `/var/lib/urep` | — |
| `UREP_ACCEPT_ROOTS` (comma-separated) | data dir + parent dirs of all `UREP_PATH` | `--root` (repeatable) |
| `UREP_LISTEN` | `0.0.0.0:7801` | `--listen` |
| `UREP_MULTICAST` | `true` | — |
| `UREP_MULTICAST_GROUP` | `239.255.42.99:7802` | — |
| `UREP_ANNOUNCE_INTERVAL` | `5s` | — |
| `UREP_PEER_TIMEOUT` | `30s` | — |
| `UREP_SEEDS` (comma-separated) | empty | — |
| `UREP_PRE_HOOK` / `UREP_POST_HOOK` | empty; shell, newline-separated | `--pre-hook` / `--post-hook` |
| `UREP_HOOKS_ENABLED` | `true` | — |
| `UREP_HOOK_TIMEOUT` | `30s` | — |
| `UREP_DEBOUNCE` | `200ms` | — |
| `UREP_ANTI_ENTROPY` | `60s` | — |
| `UREP_MAX_CLOCK_SKEW` | `5m` | — |
| `UREP_CONFLICT_WINDOW` | `2s` | — |
| `UREP_MAX_FILE_SIZE` | `16MiB` | — |
| `UREP_DIR_MODE` | `0755` | — |
| `UREP_LOG_LEVEL` | `info` | `--log-level` |

Token precedence: `--token` > `--token-file` > `UREP_TOKEN` > `UREP_TOKEN_FILE`.

### 4.2 Tags

- A message carries `tags`; a node accepts it if `set(msg.tags) ∩ set(node.tags) ≠ ∅`, or if `"*"` is on either side.
- `node.tags = []` → accepts nothing; `["*"]` → accepts everything.
- **All paths share the same tags** (node tags). There are no per-path tags.
- The sender uses the tags from ANNOUNCE; the receiver checks again.

### 4.3 Paths

- `UREP_PATH` / `--path`: individual files only, comma-separated in ENV.
- If a path contains a comma, use `--path` (repeatable).
- **With no paths at all** → a receive-only node (normal for `slave`); it logs a warning.
- The parent directory is created with `os.makedirs(UREP_DIR_MODE)` if it does not exist.
- Symlinks are skipped + a warning is logged.

### 4.4 Hooks

- **One global `pre` and one global `post` per node**, applied to all paths.
- Commands are shell strings, newline-separated; `UREP_PATH` is in the environment, so the script can branch by file:

```bash
UREP_POST_HOOK='case "$UREP_PATH" in
  */nginx.conf) nginx -s reload ;;
  */app.yaml)   systemctl restart app ;;
esac'
```

- Recommended container pattern: **one container = one file = one hook**. Then a global hook is not a limitation.
- If a hook fails: `pre` → abort + `RED`; `post` → the file remains + `YELLOW`. No retry.
- Audit: every command and exit code goes to `events.jsonl`.

### 4.5 Container and single-file mount

- Data volume `/urep`, state volume `/var/lib/urep` (required for history across restarts).
- Image: `python:3.12-slim` + `git`, entrypoint `urep`, `STOPSIGNAL SIGTERM`.
- A token on the CLI is visible in `ps`/`docker inspect`; prefer `UREP_TOKEN_FILE`.
- **A single-file bind mount** does not support `rename()`: `os.replace` returns `EBUSY`. Therefore:
  1. try `os.replace`;
  2. on `EBUSY`/`EXDEV` → **in-place write** (`O_WRONLY|O_TRUNC`, write, `fsync`, `chmod`);
  3. warning: atomicity is not guaranteed.
- A host-side atomic save on a single-file mount can leave the container attached to the old inode; `urep doctor` warns about this, and a directory mount is recommended.

**Example:**

```bash
docker run -d --name urep-app \
  -e UREP_TOKEN=123 \
  -e UREP_TAG=nginx \
  -e UREP_PATH=/urep/app.yaml \
  -e UREP_POST_HOOK='nginx -s reload' \
  -v /home/app/app.yaml:/urep/app.yaml \
  -v urep-state:/var/lib/urep \
  urep:latest
```

## 5. Discovery and membership

1. **ANNOUNCE** (UDP multicast, 5 s): `{node_id, tags, role, tcp_addr, proto, color, last_path, last_ts}`. AEAD with the token key.
2. **Seeds** (optional): direct TCP connect.
3. **Gossip**: `HELLO` carries a list of known peers with tags/status.
4. **Liveness**: `up` → `suspect` → `down` (3 misses / 45 s).

`slave` is announced the same way so peers can see it.

## 6. Status

`GREEN` · `YELLOW` (hook failed, conflict, skipped symlink, non-atomic write) · `RED` (apply/pre_hook failed, NACK, clock skew) · `PAUSED`.

```json
{"color":"GREEN","role":"peer","last_path":"/urep/app.yaml","last_peer":"web-02",
 "last_action":"update","last_hook_exit":0,"last_ms":41,"last_ts":"2026-11-04T16:22:03Z",
 "conflicts":0}
```

Status is sent in ANNOUNCE and PONG. A `YELLOW` caused by a conflict clears on the first successful apply.

## 7. Protocol

TCP, frame `[4B len][1B type][12B nonce][AEAD ciphertext]`. Max frame 1 MiB.

| Message | Content |
|---|---|
| `HELLO` / `HELLO_ACK` | proto, node_id, tags, role, nonce, HMAC challenge |
| `PING` / `PONG` | seq, sent_at, status, RTT |
| `MANIFEST_REQ` / `MANIFEST` | batch of 1000 entries `{path, h, v, m, d}` |
| `FILE_PUT` | path, version, blob, mode, size, tags, pre_hook, post_hook, content |
| `FILE_CHUNK` | transfer_id, seq, data (256 KiB) |
| `FILE_DELETE` | path, version, tags, pre_hook, post_hook |
| `FILE_GET` / `FILE_DATA` | blob (optional optimization) |
| `ACK` / `NACK` | msg_id, status, reason |
| `PEERS` | node list + tags/role/status |

- Handshake: the token is never sent; HMAC challenge.
- Keys: `HKDF-SHA256(token, salt=cluster_id) → enc_key, mac_key`.
- AAD includes `node_id` and the message type.
- `msg_id = SHA-256(type ‖ path ‖ version ‖ content_hash)`; stored in the commit trailer.
- `content_hash` = git blob OID (SHA-256 repo).
- `NACK`: `tag_mismatch`, `path_not_allowed`, `path_forbidden`, `bad_hash`, `paused`, `pre_hook_failed`, `slave_readonly`.

## 8. Git as the only source of state

There is no on-disk index or cache. State is a function of git history:

```
state(path) = the last commit that touched path
```

### 8.1 Commit format

```
urep: 2 changes from web-01

Urep-Node: web-01
Urep-Msg: 9f2c7a...e1
Urep-Time: 2026-11-04T16:22:03.412Z
Urep-Summary:
  M /urep/app.yaml
  D /urep/old.yaml
Urep-Data: {"changes":[{"p":"urep/app.yaml","o":"M","m":"0640","v":"1762359723412-0-lb-01-a1b2c3","hp":[["nginx","-t"]],"ho":[["nginx","-s","reload"]]},{"p":"urep/old.yaml","o":"D","m":"0644","v":"1762359723412-1-lb-01-a1b2c3","hp":[],"ho":[]}]}
```

Fields: `p` mirror path, `o` operation (`A`/`M`/`D`), `m` mode, `v` HLC, `hp`/`ho` hook commands.

### 8.2 Startup derivation

```
state = {}                                  # path → {h, v, m, d, hp, ho, commit, ts}

for commit in git log --raw -z --format=…   # newest-first
    data = json(Urep-Data trailer)
    for c in data.changes:
        if c.p not in state:                # the first (newest) commit wins
            state[c.p] = {h: blob_from_raw(c), v: c.v, m: c.m,
                          d: (c.o == "D"), hp: c.hp, ho: c.ho,
                          commit: sha, ts: time}
```

- One pass over history; `git log --raw` provides the blob OID and status.
- Tombstone: the last commit for the path is `D`.
- The result is an in-memory map maintained incrementally on every new commit.
- **Startup is O(number of commits)**; it grows linearly with history.

### 8.3 Consistency

- For every `d=false` blob must exist (`git cat-file -e`); `urep doctor` verifies this.
- A file on disk that is not in `state` → on a `peer` node the watcher commits it; on a `slave` node `urep doctor` reports drift.
- There is no `urep index rebuild` — every startup is a rebuild.
- `SIGHUP` re-derives state from git and re-attaches watchers.
- The anti-entropy manifest is built from the in-memory map.

### 8.4 History

- A commit is created before `post_hook`; the hook result goes to `events.jsonl` and status.
- History is kept forever; `git gc --auto` maintains the repo.

## 9. Replication: local change (peer only)

1. The watcher monitors the parent directory of every `UREP_PATH` and filters the exact filename (`IN_MODIFY`, `IN_MOVED_TO`, `IN_CREATE`, `IN_DELETE`); the parent is created if it does not exist; it re-attaches if it disappears.
2. Debounce `UREP_DEBOUNCE` (200 ms), batch up to 2 s.
3. Re-hash the entire file (editors do temp+rename).
4. Hash equals the in-memory state → ignore (echo suppression).
5. Write to the mirror + commit + update the in-memory map.
6. `FILE_PUT` to peers whose tags match.

Local changes do not trigger hooks. Symlinks are skipped + a warning is logged.

## 10. Apply: received change

```
lock(path)
  if path in forbidden:                    return NACK(path_forbidden)
  if not under accept_roots:               return NACK(path_not_allowed)
  if realpath(parent) not under roots:     return NACK(path_not_allowed)
  if not tag_match(msg.tags, node.tags):   return NACK(tag_mismatch)
  if blob != hash(content):                return NACK(bad_hash)
  if paused:                               return NACK(paused)

  local = state.get(path)
  if local is None and file exists:
      snapshot: commit local state as "urep: pre-join snapshot"

  if local and remote.version <= local.version:   return ACK(noop)
  if local and |remote.wall - local.wall| < conflict_window and content differs:
      mark YELLOW(conflict) + conflict ref

  os.makedirs(parent, dir_mode, exist_ok=True)
  pre_hook (once per batch) || return NACK(pre_hook_failed) + RED
  write (4.5: replace or in-place fallback) + chmod
  mirror write + commit(remote attribution) + update in-memory map
  post_hook (once per batch) → GREEN/YELLOW
```

- LWW: HLC triples `(wall_ms, counter, node_id)` lexicographically.
- One lock per path; hooks never overlap.
- Hook environment: `UREP_PATH`, `UREP_ACTION`, `UREP_PEER`, `UREP_VERSION`, `UREP_MODE`.

## 11. Deletion

- Local `IN_DELETE`/`IN_MOVED_FROM` (peer only) → commit `D` + new HLC.
- `FILE_DELETE` → if the version is newer, `os.unlink` + commit `D` (tombstone in history).
- Hooks are also executed on deletion (`UREP_ACTION=delete`).
- The tombstone lives in history forever.

## 12. Conflicts

- The winner is the higher HLC — deterministic.
- If the versions are closer than `UREP_CONFLICT_WINDOW` (2 s) and the content differs → `YELLOW` + ref:

```
git update-ref refs/urep/conflict/<hexpath>/<hlc> <losing_commit>
```

- `urep conflicts` / `urep restore <ref>`.
- `YELLOW` clears on the first successful apply.

## 13. Anti-entropy and join

- On connect and periodically (`UREP_ANTI_ENTROPY`, 60 s): `MANIFEST` batches from the in-memory map.
- Diff by `(path, h, v)` → the newer side sends `FILE_PUT` with hooks from the map.
- An applied remote change is forwarded onward **except to the sender**, so it reaches nodes that are not directly connected.
- Files >900 KB go through `FILE_CHUNK` (256 KB); the upper bound is `UREP_MAX_FILE_SIZE`.
- New node: the local file is snapshotted, then the cluster wins.
- `slave` may send `MANIFEST_REQ` + receive; it never sends files.
- Hooks also run on catch-up; `urep sync --no-hooks` skips hooks for one sync.
- Manifest for 10k files ~1 MB → batch of 200.

## 14. Pause / resume

```
urep pause [--in|--out] [--for 10m]
urep resume
```

- Default: pauses both directions.
- Watcher and commit continue (on a `peer` node) — changes are recorded, not sent.
- The peer receives `NACK paused`; it marks `PAUSED`.
- State in `state.json` (survives restart).
- `resume` starts anti-entropy — there is no queue.
- Discovery/ping stay active.

## 15. CLI

```
urep [--token T] [--prefix P] [--tag T]... [--path P]... [--root R]...
     [--slave] [--listen ADDR] [--pre-hook CMD] [--post-hook CMD]
     # without a subcommand = daemon

urep init [--token-file PATH] [--print-token]
urep id [--prefix P]              # generates a node ID without starting the daemon
urep ping HOST:PORT               # handshake + PING/PONG
urep daemon [-f] [--slave]
urep status [--json]
urep peers [--watch]
urep config [--json]              # effective configuration, token redacted
urep pause [--in|--out] [--for 10m]
urep resume
urep log [PATH] [-n 20]
urep show REV
urep diff REV [PATH]
urep revert REV
urep conflicts
urep restore REF
urep sync [--path PATH] [--peer NODE] [--force] [--no-hooks]
urep events [-f]
urep doctor
urep gc
urep version
```

- `urep` without a subcommand = `urep daemon` (container entrypoint).
- `--token` is the only required flag; alternatives `UREP_TOKEN`, `UREP_TOKEN_FILE`, `--token-file`.
- The node ID is a disposable UUID generated at startup; `--prefix` only adds a readable prefix.
- There is no `path add/remove`, no `root add/remove`, no `--config` — configuration is env/CLI and changes require a restart.
- CLI ↔ daemon through `run/urep.sock`. `SIGHUP` re-derives, `SIGTERM` is a graceful shutdown.

## 16. Security

- Token: 0600, never in logs. In a container prefer `UREP_TOKEN_FILE`.
- Encryption of all messages, including discovery.
- Write allowlist: `UREP_ACCEPT_ROOTS` + `realpath(parent)` + deny list.
- Tag checks on both sides.
- Hooks = RCE by design (accepted). Mitigations:
  - the command is bound to a file change — there is no "EXEC" message;
  - `UREP_HOOKS_ENABLED=false`;
  - timeout + max length;
  - audit in `events.jsonl`.
- The container can run as non-root if it writes only to `/urep` and does not use hooks.

## 17. Filesystem

- Only explicitly listed files; no recursion, globs, or auto-scan.
- The watcher monitors the parent directory and filters the exact filename; the parent is created.
- Symlink: `lstat` → skip + warning.
- Special files: skip + warning.
- `fs.inotify.max_user_watches`; `urep doctor` checks it.
- Metadata: content + mode. Ownership stays local.

## 18. Limits and performance

- File ≤ 16 MiB, chunk 256 KiB, frame ≤ 1 MiB.
- Target: < 500 ms LAN latency, hundreds of nodes, thousands of files.
- Rate limit 50 messages/s per peer; one apply per path.
- Startup is O(number of commits) due to derivation; `urep doctor` measures the time.
- Clock skew > `UREP_MAX_CLOCK_SKEW` (5 min): `RED`, rejects apply, logs.
- HLC never goes backwards: `wall_ms = max(now, last_wall)`.

## 19. Stack

Python 3.11+ · `asyncio` · `inotify_simple` · `cryptography` · `cbor2` · `subprocess` + system `git ≥ 2.29` (SHA-256 repo) · `pydantic` (env validation) · `typer` · `structlog` · `pytest` + `hypothesis` · `uv` · Docker.

**All development and testing go through Docker** — host Python is not used (see 25).

## 20. Testing

**All tests run in Docker.** `pytest` on the host is not supported; the host may only use `docker` and `make`.

- **Unit:** HLC, LWW, state derivation from synthetic history, frame codec, tag matching, hook timeout, env precedence, UUID node ID.
- **Integration:** 3-node cluster; tag filtering; `slave` receives but does not send; delete → tombstone from history; new node → snapshot + backfill; parent dir is created.
- **Container:** ENV-only start; `-v file:file` → in-place fallback; `-v dir:/urep` → atomic write; restart with state volume.
- **Chaos:** kill -9 during apply (derivation must return consistent state), partition, clock skew, duplicate packets. Requires `NET_ADMIN`, marked `-m chaos`.
- **Fuzz:** frame decoder, malformed CBOR, overlong paths, malformed `Urep-Data` JSON.
- **Security:** invalid token, replay, path traversal, `accept_roots` escape, tag mismatch.

Commands: `make test`, `make test-unit`, `make test-integration`, `make test-chaos`, `make lint`, `make build`. CI uses the same `Dockerfile.dev` — no `setup-python` on the host.

## 21. Phases

| Phase | Content |
|---|---|
| M0 | **Docker dev image (Python + git + test tools), compose harness, `make` targets, CI** |
| M1 | skeleton, env/CLI config, git repo, state derivation from history, HLC, watcher + commit |
| M2 | transport + handshake + AEAD |
| M3 | multicast discovery + tags/role + gossip + status/ping |
| M4 | FILE_PUT/apply, tag match, accept_roots, LWW, atomic write + fallback, ACK/NACK |
| M5 | hooks (global, batch coalescing), events, status colors, delete hooks |
| M6 | tombstone, conflicts + YELLOW, anti-entropy, join/backfill |
| M7 | `--slave`, pause/resume, CLI, Docker image, systemd |
| M8 | chaos/fuzz, hardening, documentation |

## 22. Open questions (for v2)

Key rotation · NAT relay · bandwidth limit · uid/gid/xattr · symlink replication · directories · per-path tags/hooks · runtime configuration changes · TUI dashboard · `urep gc` tombstone policy · Kubernetes manifest · optional state cache if history becomes too large.

---

## 23. Decision changelog

**v1.0 → v1.1:** symlinks in v1, first join cluster wins, hooks on catch-up/delete, coalescing, conflicts + YELLOW, drop-in config, argv+shell hooks.

**v1.1 → v1.2:** tags as a subscription, `accept_roots`, `--slave`, symlinks and directories out of v1, parent dir is created.

**v1.2 → v1.3:** ENV mode without TOML, `--token` the only required flag, default tag `default`, no auto-scan, container-first, single-file mount fallback.

**v1.3 → v1.4:** `versions.json` removed; state is derived from git history; `Urep-Data` JSON trailer.

**v1.4 → v1.5:**
- **Config files completely removed.** No `urep.toml`, no `paths.d/`, no `--config`.
- Configuration exclusively CLI + `UREP_*` env; precedence CLI > ENV > default.
- `urep path add/remove` and `urep root add/remove` removed — changes go through restart.
- Tags become global for all paths; hooks are also global (`UREP_PRE_HOOK`/`UREP_POST_HOOK`), with `UREP_PATH` in the env for branching.
- Added `urep config` to show the effective configuration.
- Recommended pattern: one container = one file = one hook.

**v1.5 → v1.6:** node ID is a disposable UUID at startup (not persisted), `--prefix` for readability, `urep id` generator.

**v1.6 → v1.7:**
- **All development and testing exclusively in Docker**; host Python is not supported.
- Added `Dockerfile` (runtime: Python + git) and `Dockerfile.dev` (Python + git + test tools).
- `docker-compose.dev.yml` / `docker-compose.test.yml`, `make` targets, CI without `setup-python`.
- Multicast note: Docker bridge does not forward multicast; integration uses `UREP_SEEDS`, `-m multicast` goes on the host network.
- M0 added as a mandatory first step.

**v1.7 → v1.8 (implemented M2–M4):**
- **M2:** token handshake (HMAC challenge, the token is not sent), HKDF + ChaCha20-Poly1305 frame encryption, TCP transport, `urep ping`.
- **M3:** UDP multicast discovery with AEAD, gossip, tags/role, `peers.json`, `urep peers [--watch]`.
- **M4:** `FILE_PUT` / `FILE_DELETE`, chunking, LWW apply, `accept_roots`, tag checks, pre/post hooks on the receiver, tombstone deletion.
- Manifest anti-entropy on connect and periodically; applied changes are forwarded onward through the mesh (except to the sender).
- `urep status` color is derived from the last event (`GREEN`/`YELLOW`/`RED`/`PAUSED`).
- Hooks are still per-file; batch coalescing remains for M5.

## 24. Notes before development

1. **Git is a hard dependency** — `urep doctor` checks `git >= 2.29` and `--object-format=sha256`.
2. **Hooks as root RCE** is a deliberate decision; `UREP_HOOKS_ENABLED=false` is a single switch to turn them off.
3. **`UREP_ACCEPT_ROOTS` is the only write boundary** — a root that is too broad means the token holder can write anywhere under it.
4. **A single-file mount is not atomic** — a directory mount is recommended.
5. **Startup grows with history** — if it becomes a problem, v2 can add an optional cache.
6. **One container = one file** is the cleanest model; multiple files share the same tags and hooks.
7. **The node ID is disposable** — after a restart the node looks new; there is no way to pin it.
8. **Host Python is not supported** — everything goes through Docker; `make test` is the only entry point.

---

## 25. Development and testing exclusively in Docker

**Rule:** no development, testing, or build step runs on the host. No host `python`, no host `.venv`, no `pytest` outside the container. The host may only use `docker` and `make`.

### 25.1 Images

- **`Dockerfile` (runtime):** `python:3.12-slim-bookworm` + `git` + `ca-certificates`; multi-stage; `uv sync --frozen --no-dev`; `ENTRYPOINT ["urep"]`; `STOPSIGNAL SIGTERM`; volumes `/urep`, `/var/lib/urep`; `git >= 2.29` (Debian bookworm has 2.39).
- **`Dockerfile.dev` (dev/test):** the same base + `uv`, `pytest`, `ruff`, `mypy`, `iproute2`, `procps`, `iptables`; `UV_PROJECT_ENVIRONMENT=/opt/venv` (so the bind mount does not hide the venv); default `CMD ["pytest"]`.
- **`docker-compose.dev.yml`:** the `dev` service with a repo bind mount (`.:/src`) and a state volume.
- **`docker-compose.test.yml`:** `node-a`, `node-b`, `node-c`, `test` on the same network.

### 25.2 Networking in tests

Docker bridge **does not forward multicast**. Therefore:

- integration tests use `UREP_SEEDS` (deterministic), not multicast;
- a separate `-m multicast` test uses `network_mode: host` (Linux) or macvlan and is opt-in;
- chaos tests (partition) require `cap_add: [NET_ADMIN]` and are marked `-m chaos`.

### 25.3 Commands

```
make dev                 # bash in the dev container
make test                # all tests in Docker
make test-unit
make test-integration
make test-chaos
make lint                # ruff + mypy
make build               # runtime image
make smoke               # run the image and check `urep doctor`
```

All targets are thin wrappers around `docker compose run --rm` / `docker build`.

### 25.4 CI

- One job: `docker build -f Dockerfile.dev -t urep-dev .` → `docker run --rm urep-dev pytest`.
- Build the runtime image and smoke test: `docker run --rm urep:ci --token test --path /tmp/x` + `urep doctor`.
- No `actions/setup-python`; no host dependency installation.

### 25.5 Acceptance criteria

- `docker build -f Dockerfile.dev .` passes without any host dependency other than Docker.
- `make test` passes on a clean host.
- The runtime image contains `git >= 2.29` and supports `--object-format=sha256`.
- Integration tests bring up 3 nodes and prove convergence.
- No test requires host Python or host `pytest`.
