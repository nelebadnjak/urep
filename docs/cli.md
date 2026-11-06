# uRep — CLI examples (v1.6)

A practical cheat sheet for configuration without a config file: just CLI + `UREP_*` env.
Precedence: **CLI > ENV > default**. No `urep.toml`, no `paths.d/`, no `--config`.

---

## 1. Starting

```bash
# everything via CLI — a "peer" that sends and receives
urep --token 123 \
     --tag nginx \
     --path /etc/nginx/nginx.conf \
     --post-hook 'nginx -t && nginx -s reload'

# the same via ENV
export UREP_TOKEN=123
export UREP_TAG=nginx
export UREP_PATH=/etc/nginx/nginx.conf
export UREP_POST_HOOK='nginx -t && nginx -s reload'
urep

# slave: only receives, does not even need UREP_PATH
UREP_TOKEN=123 UREP_TAG=nginx UREP_ROLE=slave urep

# foreground for debugging
urep daemon -f --token 123 --tag nginx --path /app.yaml
```

`urep` without a subcommand = `urep daemon`.

### Flags

```bash
urep [--token T] [--prefix P] [--tag T]... [--path P]... [--root R]...
     [--slave] [--listen ADDR] [--pre-hook CMD] [--post-hook CMD]
```

- `--token` is the only required flag.
- Alternatives: `--token-file`, `UREP_TOKEN`, `UREP_TOKEN_FILE`.
- **No `--name`** — the node ID is a disposable UUID generated at startup and is not persisted.
- `--prefix` gives a readable ID: `<prefix>-<uuid>` (for example `bg-3f9a1c2b-...`).
- `--tag` and `--path` are repeatable; in ENV they are comma-separated.
- Token precedence: `--token` > `--token-file` > `UREP_TOKEN` > `UREP_TOKEN_FILE`.

---

## 2. Docker

```bash
# peer
docker run -d --name urep-app \
  -e UREP_TOKEN=123 \
  -e UREP_TAG=nginx \
  -e UREP_PATH=/urep/app.yaml \
  -e UREP_POST_HOOK='nginx -s reload' \
  -v /home/app/app.yaml:/urep/app.yaml \
  -v urep-state:/var/lib/urep \
  urep:latest

# slave
docker run -d --name urep-app-slave \
  -e UREP_TOKEN=123 \
  -e UREP_TAG=nginx \
  -e UREP_ROLE=slave \
  -v /home/app/app.yaml:/urep/app.yaml \
  -v urep-state-slave:/var/lib/urep \
  urep:latest
```

Instead of the token in an env var (visible in `docker inspect`), use a Docker secret:

```bash
docker run -d --name urep-app \
  --secret urep_token \
  -e UREP_TOKEN_FILE=/run/secrets/urep_token \
  -e UREP_TAG=nginx \
  -e UREP_PATH=/urep/app.yaml \
  -v /home/app/app.yaml:/urep/app.yaml \
  -v urep-state:/var/lib/urep \
  urep:latest
```

Recommendation: **one container = one file = one hook**.

---

## 3. Init and token

```bash
urep init --print-token
# Generated /etc/urep/token (0600)
# UREP_TOKEN=8f3c1d...

urep init --token-file /etc/urep/token
```

Name generator without starting the daemon:

```bash
urep id
# 3f9a1c2b-7d4e-4a8f-9c1d-2e3f4a5b6c7d

urep id --prefix bg
# bg-3f9a1c2b-7d4e-4a8f-9c1d-2e3f4a5b6c7d
```

`urep id` is informational only — the daemon always generates a new UUID at startup and does not accept one from outside.

## 3.1 Ping

```bash
urep ping 10.0.0.12:7801
# pong from 3f9a1c2b-7d4e-4a8f-9c1d-2e3f4a5b6c7d role=peer tags=test rtt=1.2ms
```

The token is read from `UREP_TOKEN` / `UREP_TOKEN_FILE` (or `--token`). `ping` verifies the handshake and AEAD without waiting for discovery.

---

## 4. State overview

```bash
urep status
# node   lb-01-a1b2c3
# role   peer
# state  GREEN
# tags   nginx
# paths  /etc/nginx/nginx.conf
# last   update /etc/nginx/nginx.conf from web-02 (41 ms, hook exit 0)
# peers  4 up, 0 suspect, 1 down

urep status --json
```

```bash
urep peers
# NODE       ROLE   TAGS        ADDR            STATE   RTT    COLOR   LAST
# web-02     peer   web,edge    10.0.0.12:7801  up      1.2ms  GREEN   12s ago
# db-01      peer   db          10.0.0.20:7801  up      0.8ms  YELLOW  3m ago
# cache-01   slave  cache       10.0.0.30:7801  up      0.6ms  GREEN   8s ago
# old-01     peer   web         10.0.0.9:7801   down    —      —       —

urep peers --watch
```

```bash
urep config
# token        **** (from UREP_TOKEN)
# role         peer
# tags         nginx
# paths        /etc/nginx/nginx.conf
# accept_roots /urep, /etc/nginx
# listen       0.0.0.0:7801
# multicast    239.255.42.99:7802
# state_dir    /var/lib/urep
# repo_dir     /var/lib/urep/repo
```

`urep config` never prints the token in clear text.

---

## 5. Debug

```bash
urep pause                    # pauses both directions
urep pause --in               # receive only
urep pause --out --for 10m    # send only, 10 minutes
urep resume                   # starts anti-entropy immediately
```

```bash
urep events -f
# 16:22:03.412 web-02  apply  /etc/nginx/nginx.conf  M  hook exit 0 (41ms)
# 16:22:05.001 web-02  skip   /etc/nginx/nginx.conf  noop (same version)
```

---

## 6. History (git)

```bash
urep log
urep log /etc/nginx/nginx.conf -n 3
# a1b2c3d  2026-11-04 16:22  web-02  M  /etc/nginx/nginx.conf
# 9f2c7a1  2026-11-04 15:10  web-01  M  /etc/nginx/nginx.conf

urep show a1b2c3d              # commit + diff
urep diff a1b2c3d /etc/nginx/nginx.conf
urep revert a1b2c3d            # new commit; replicates like any other change
```

---

## 7. Conflicts

```bash
urep conflicts
# CONFLICT  /etc/nginx/nginx.conf
# ref       refs/urep/conflict/2f1a.../1762359723412-0-web-01-a1b2c3
# reason    concurrent edits within 2s; remote HLC won

urep restore refs/urep/conflict/2f1a.../1762359723412-0-web-01-a1b2c3
```

---

## 8. Manual synchronization

```bash
urep sync
urep sync --peer web-02
urep sync --path /etc/nginx/nginx.conf --force
urep sync --no-hooks           # apply without running hooks
```

---

## 9. Diagnostics

```bash
urep doctor
# git       2.43.0 (sha256 ok)
# inotify   65536 watches
# clock     skew 0.4s
# multicast 239.255.42.99:7802 reachable
# mount     single-file, atomic write NOT guaranteed (EBUSY fallback active)
# roots     /urep, /etc/nginx
# state     derived 184 commits in 0.21s

urep gc
urep version
```

---

## 10. What deliberately does not exist

```bash
urep path add ...     # none — paths via --path / UREP_PATH
urep root add ...     # none — UREP_ACCEPT_ROOTS
urep --config ...     # none — there is no config file
```

Configuration changes = restart the process/container with new env or flags.

---

## 11. ENV reference

| ENV | Default | CLI |
|---|---|---|
| `UREP_TOKEN` | — | `--token` |
| `UREP_TOKEN_FILE` | — | `--token-file` |
| `UREP_PREFIX` | — | `--prefix` |
| `UREP_TAG` | `default` | `--tag` |
| `UREP_ROLE` | `peer` | `--slave` |
| `UREP_PATH` | empty → receive-only | `--path` |
| `UREP_DATA_DIR` | `/urep` | — |
| `UREP_STATE_DIR` | `/var/lib/urep` | — |
| `UREP_ACCEPT_ROOTS` | data dir + parent dirs of paths | `--root` |
| `UREP_LISTEN` | `0.0.0.0:7801` | `--listen` |
| `UREP_MULTICAST` | `true` | — |
| `UREP_MULTICAST_GROUP` | `239.255.42.99:7802` | — |
| `UREP_ANNOUNCE_INTERVAL` | `5s` | — |
| `UREP_PEER_TIMEOUT` | `30s` | — |
| `UREP_SEEDS` | empty | — |
| `UREP_PRE_HOOK` / `UREP_POST_HOOK` | empty | `--pre-hook` / `--post-hook` |
| `UREP_HOOKS_ENABLED` | `true` | — |
| `UREP_HOOK_TIMEOUT` | `30s` | — |
| `UREP_DEBOUNCE` | `200ms` | — |
| `UREP_ANTI_ENTROPY` | `60s` | — |
| `UREP_MAX_CLOCK_SKEW` | `5m` | — |
| `UREP_CONFLICT_WINDOW` | `2s` | — |
| `UREP_MAX_FILE_SIZE` | `16MiB` | — |
| `UREP_DIR_MODE` | `0755` | — |
| `UREP_LOG_LEVEL` | `info` | `--log-level` |
