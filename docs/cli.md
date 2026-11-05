# uRep — CLI primer (v1.6)

Praktičan cheat sheet za konfiguraciju bez config fajla: samo CLI + `UREP_*` env.
Precedence: **CLI > ENV > default**. Nema `urep.toml`, nema `paths.d/`, nema `--config`.

---

## 1. Pokretanje

```bash
# sve kroz CLI — "peer" koji šalje i prima
urep --token 123 \
     --tag nginx \
     --path /etc/nginx/nginx.conf \
     --post-hook 'nginx -t && nginx -s reload'

# isto kroz ENV
export UREP_TOKEN=123
export UREP_TAG=nginx
export UREP_PATH=/etc/nginx/nginx.conf
export UREP_POST_HOOK='nginx -t && nginx -s reload'
urep

# slave: samo prima, ne mora ni UREP_PATH
UREP_TOKEN=123 UREP_TAG=nginx UREP_ROLE=slave urep

# foreground za debug
urep daemon -f --token 123 --tag nginx --path /app.yaml
```

`urep` bez podkomande = `urep daemon`.

### Flagovi

```bash
urep [--token T] [--prefix P] [--tag T]... [--path P]... [--root R]...
     [--slave] [--listen ADDR] [--pre-hook CMD] [--post-hook CMD]
```

- `--token` je jedini obavezan flag.
- Alternative: `--token-file`, `UREP_TOKEN`, `UREP_TOKEN_FILE`.
- **Nema `--name`** — node ID je disposable UUID po startu, ne čuva se.
- `--prefix` daje čitljiv ID: `<prefix>-<uuid>` (npr. `bg-3f9a1c2b-...`).
- `--tag` i `--path` su repeatable; u ENV-u su comma-separated.
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

Umesto tokena u env-u (vidljiv u `docker inspect`), koristi Docker secret:

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

Preporuka: **jedan kontejner = jedan fajl = jedan hook**.

---

## 3. Init i token

```bash
urep init --print-token
# Generated /etc/urep/token (0600)
# UREP_TOKEN=8f3c1d...

urep init --token-file /etc/urep/token
```

Generator imena bez pokretanja daemona:

```bash
urep id
# 3f9a1c2b-7d4e-4a8f-9c1d-2e3f4a5b6c7d

urep id --prefix bg
# bg-3f9a1c2b-7d4e-4a8f-9c1d-2e3f4a5b6c7d
```

`urep id` je samo informativno — daemon uvek generiše novi UUID pri startu i ne prima ga spolja.

---

## 4. Pregled stanja

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

`urep config` nikad ne ispisuje token u clear-u.

---

## 5. Debug

```bash
urep pause                    # pauzira oba smera
urep pause --in               # samo prijem
urep pause --out --for 10m    # samo slanje, 10 minuta
urep resume                   # odmah pokreće anti-entropy
```

```bash
urep events -f
# 16:22:03.412 web-02  apply  /etc/nginx/nginx.conf  M  hook exit 0 (41ms)
# 16:22:05.001 web-02  skip   /etc/nginx/nginx.conf  noop (same version)
```

---

## 6. Istorija (git)

```bash
urep log
urep log /etc/nginx/nginx.conf -n 3
# a1b2c3d  2026-11-04 16:22  web-02  M  /etc/nginx/nginx.conf
# 9f2c7a1  2026-11-04 15:10  web-01  M  /etc/nginx/nginx.conf

urep show a1b2c3d              # commit + diff
urep diff a1b2c3d /etc/nginx/nginx.conf
urep revert a1b2c3d            # novi commit; replikuje se kao i svaka izmena
```

---

## 7. Konflikti

```bash
urep conflicts
# CONFLICT  /etc/nginx/nginx.conf
# ref       refs/urep/conflict/2f1a.../1762359723412-0-web-01-a1b2c3
# reason    concurrent edits within 2s; remote HLC won

urep restore refs/urep/conflict/2f1a.../1762359723412-0-web-01-a1b2c3
```

---

## 8. Ručna sinhronizacija

```bash
urep sync
urep sync --peer web-02
urep sync --path /etc/nginx/nginx.conf --force
urep sync --no-hooks           # primeni bez pokretanja hook-ova
```

---

## 9. Dijagnostika

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

## 10. Šta namerno ne postoji

```bash
urep path add ...     # nema — putanje kroz --path / UREP_PATH
urep root add ...     # nema — UREP_ACCEPT_ROOTS
urep --config ...     # nema — nema config fajla
```

Izmena konfiguracije = restart procesa/kontejnera sa novim env-om ili flagovima.

---

## 11. ENV referenca

| ENV | Default | CLI |
|---|---|---|
| `UREP_TOKEN` | — | `--token` |
| `UREP_TOKEN_FILE` | — | `--token-file` |
| `UREP_PREFIX` | — | `--prefix` |
| `UREP_TAG` | `default` | `--tag` |
| `UREP_ROLE` | `peer` | `--slave` |
| `UREP_PATH` | prazno → receive-only | `--path` |
| `UREP_DATA_DIR` | `/urep` | — |
| `UREP_STATE_DIR` | `/var/lib/urep` | — |
| `UREP_ACCEPT_ROOTS` | data dir + parent dirs putanja | `--root` |
| `UREP_LISTEN` | `0.0.0.0:7801` | `--listen` |
| `UREP_MULTICAST_GROUP` | `239.255.42.99:7802` | — |
| `UREP_SEEDS` | prazno | — |
| `UREP_PRE_HOOK` / `UREP_POST_HOOK` | prazno | `--pre-hook` / `--post-hook` |
| `UREP_HOOKS_ENABLED` | `true` | — |
| `UREP_DEBOUNCE` | `200ms` | — |
| `UREP_ANTI_ENTROPY` | `60s` | — |
| `UREP_MAX_CLOCK_SKEW` | `5m` | — |
| `UREP_CONFLICT_WINDOW` | `2s` | — |
| `UREP_MAX_FILE_SIZE` | `16MiB` | — |
| `UREP_DIR_MODE` | `0755` | — |
| `UREP_LOG_LEVEL` | `info` | `--log-level` |
