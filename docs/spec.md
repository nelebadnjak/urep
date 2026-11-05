# uRep — specifikacija v1.8

**micro replication** — decentralizovani P2P sync agent za konfiguracione fajlove. Bez lidera, bez baze, sa lokalnim git-om kao transakcionom istorijom.

Status: nacrt odobren za razvoj
Datum: 2026-11-04
Jezik implementacije: Python 3.11+

---

## 0. Odluke

| Tema | Odluka |
|---|---|
| Topologija | P2P mesh, bez lidera, LAN / isti subnet |
| Discovery | UDP multicast + gossip (seed lista opciona) |
| Auth | jedan cluster token → HKDF → ChaCha20-Poly1305, bez PKI |
| Targeting | tagovi kao pretplata: nod sluša poruke sa svojim tagovima |
| Konzistentnost | eventualna + HLC LWW, konflikt se markira `YELLOW` |
| State | **git je jedini izvor**; stanje se izvodi iz istorije pri startu, bez indeksa |
| Node ID | disposable UUID po startu, ne čuva se; `--prefix` samo za čitljivost |
| Git | sistemski `git` (subprocess), SHA-256 repo, istorija zauvek |
| Putanje | samo eksplicitno navedeni pojedinačni fajlovi; metadata = sadržaj + mode |
| Symlink | van v1 — preskoči + warning |
| Hooks | putuju uz fajl, izvršava samo receiver, na update/catch-up/delete |
| Brisanje | tombstone (git delete commit), hooks se izvršavaju |
| Režim rada | `peer` (šalje + prima) ili `slave` (samo prima, sme da pull-uje) |
| Konfiguracija | **samo CLI + ENV; nema config fajla** |
| Deployment | container-first: Docker volume + ENV |
| Status | node-level boja + poslednja akcija |
| Kontrola | `urep pause` / `urep resume` |

## 1. Ciljevi / non-goals

**Ciljevi:** G1 P2P bez lidera · G2 autodiscovery · G3 token auth+enkripcija · G4 tag pretplata · G5 watch→push · G6 hash po poruci · G7 pre/post hook · G8 eventualna konzistentnost + LWW · G9 CLI+daemon · G10 git istorija · G11 pause/resume · G12 `--slave` režim · G13 container bez config fajla · G14 nula dodatnog stanja (samo git).

**Non-goals (v1):** GUI · binarni fajlovi >16 MiB · multi-writer merge (CRDT) · NAT traversal/relay · key rotation · Windows · uid/gid/xattr/ACL/SELinux · symlink-ovi · rekurzivni direktorijumi · auto-scan direktorijuma · prazni direktorijumi · **config fajlovi (TOML/YAML/JSON)** · per-path tagovi · per-path hooks · runtime izmena konfiguracije.

## 2. Arhitektura

```
┌──────────────────────────── uRep daemon (jedan po nodu) ──────────────────────────────────┐
│                                                                                            │
│ watcher ──► commit ──► replicator ──► transport (TCP+AEAD) ◄──► discovery (UDP multicast) │
│ (samo peer)    │            │  ▲              │                        + gossip             │
│                ▼            ▼  │              ▼                                            │
│         repo/ (git) ◄──── apply ──► write ──► hooks ──► status/events                      │
└────────────────────────────────────────────────────────────────────────────────────────────┘
                ▲
        urep CLI ── unix socket ── /var/lib/urep/run/urep.sock
```

- **Nema koordinatora**, nema kvoruma, nema izbora lidera.
- **Nema trajnog reda poruka.** Posle particije se stanje poravnava anti-entropy manifest diff-om.
- **Nema baze, nema indeksa, nema config fajla.** `state.json` je samo `node_id`/`paused`; `events.jsonl` je log.
- **Sve je idempotentno**: ista poruka se odbija jer verzija nije novija, ili jer je sadržaj isti.

## 3. Layout na disku

```
/var/lib/urep/                     # UREP_STATE_DIR
├── repo/                          # git ogledalo (0700, SHA-256 repo)
│   ├── .git/
│   └── urep/app.yaml              # ogledalo za /urep/app.yaml
├── events.jsonl                   # append-only, rotacija
├── state.json                     # paused (disposable)
└── run/urep.sock

/etc/urep/token                    # opciono: token fajl (0600)
/urep/                             # UREP_DATA_DIR (container data volume)
```

Git **nikad** ne dobija `--work-tree=/`. Stvarni fajlovi se pišu isključivo našim atomic write-om (uz container fallback, 4.5).

**Node ID (disposable):**

- Uvek `uuid4()`, generisan pri svakom startu; **nikad se ne čuva**.
- `UREP_PREFIX` / `--prefix` → `<prefix>-<uuid4()>` (npr. `bg-3f9a1c2b-...`), samo radi čitljivosti (site/hostname deo).
- Nema korisničkog imena; identitet se ne može fiksirati.
- `urep id [--prefix P]` samo ispiše ID koji bi bio generisan (informativno).
- Posle restarta nod ima novi ID; peer-ovi ga vide kao novi nod, a stari zapis istekne posle `peer_timeout` (30 s).
- Kolizija UUID4 je praktično nemoguća; nema korisničkih imena pa nema duplikata.
- Node ID ulazi u HLC tiebreak i u `Urep-Node` commit trailer.

**Zabranjene putanje (hard deny):** `/etc/urep`, `/var/lib/urep`, `/proc`, `/sys`, `/dev`, `/run`, repo direktorijum, i sve što posle normalizacije sadrži `..`.

## 4. Konfiguracija

**Nema config fajla.** Sve dolazi iz CLI flagova ili `UREP_*` env varijabli. Redosled: **CLI > ENV > built-in default.** Liste se zamenjuju, ne spajaju.

### 4.1 ENV tabela

| ENV | Default | CLI |
|---|---|---|
| `UREP_TOKEN` | — (obavezno ako nema fajla) | `--token` |
| `UREP_TOKEN_FILE` | — | `--token-file` |
| `UREP_PREFIX` | — | `--prefix` |
| `UREP_TAG` (comma-separated) | `default` | `--tag` (repeatable) |
| `UREP_ROLE` (`peer`/`slave`) | `peer` | `--slave` |
| `UREP_PATH` (comma-separated) | prazno → receive-only | `--path` (repeatable) |
| `UREP_DATA_DIR` | `/urep` | — |
| `UREP_STATE_DIR` | `/var/lib/urep` | — |
| `UREP_ACCEPT_ROOTS` (comma-separated) | data dir + parent dirs svih `UREP_PATH` | `--root` (repeatable) |
| `UREP_LISTEN` | `0.0.0.0:7801` | `--listen` |
| `UREP_MULTICAST` | `true` | — |
| `UREP_MULTICAST_GROUP` | `239.255.42.99:7802` | — |
| `UREP_ANNOUNCE_INTERVAL` | `5s` | — |
| `UREP_PEER_TIMEOUT` | `30s` | — |
| `UREP_SEEDS` (comma-separated) | prazno | — |
| `UREP_PRE_HOOK` / `UREP_POST_HOOK` | prazno; shell, newline-separated | `--pre-hook` / `--post-hook` |
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

### 4.2 Tagovi

- Poruka nosi `tags`; nod prima ako `set(msg.tags) ∩ set(node.tags) ≠ ∅`, ili ako je `"*"` na bilo kojoj strani.
- `node.tags = []` → ne prima ništa; `["*"]` → prima sve.
- **Sve putanje dele iste tagove** (node tagove). Nema per-path tagova.
- Sender koristi tagove iz ANNOUNCE-a; receiver ponovo proverava.

### 4.3 Putanje

- `UREP_PATH` / `--path`: samo pojedinačni fajlovi, comma-separated u ENV-u.
- Ako putanja sadrži zapetu, koristi `--path` (repeatable).
- **Bez ijedne putanje** → receive-only čvor (normalno za `slave`); loguje warning.
- Parent direktorijum se kreira `os.makedirs(UREP_DIR_MODE)` ako ne postoji.
- Symlink se preskače + warning.

### 4.4 Hooks

- **Jedan globalni `pre` i jedan globalni `post` po nodu**, primenjuju se na sve putanje.
- Komande su shell stringovi, newline-separated; `UREP_PATH` je u env-u, pa skripta može da grana po fajlu:

```bash
UREP_POST_HOOK='case "$UREP_PATH" in
  */nginx.conf) nginx -s reload ;;
  */app.yaml)   systemctl restart app ;;
esac'
```

- Preporučeni container pattern: **jedan kontejner = jedan fajl = jedan hook**. Tako globalni hook i nije ograničenje.
- Ako hook padne: `pre` → abort + `RED`; `post` → fajl ostaje + `YELLOW`. Bez retry-ja.
- Audit: svaka komanda i exit code idu u `events.jsonl`.

### 4.5 Container i single-file mount

- Data volume `/urep`, state volume `/var/lib/urep` (obavezan za istoriju preko restarta).
- Image: `python:3.12-slim` + `git`, entrypoint `urep`, `STOPSIGNAL SIGTERM`.
- Token u CLI-ju je vidljiv u `ps`/`docker inspect`; preporuka `UREP_TOKEN_FILE`.
- **Single-file bind mount** ne podržava `rename()`: `os.replace` vraća `EBUSY`. Zato:
  1. pokušaj `os.replace`;
  2. na `EBUSY`/`EXDEV` → **in-place write** (`O_WRONLY|O_TRUNC`, write, `fsync`, `chmod`);
  3. warning: atomičnost nije garantovana.
- Host-side atomic save na single-file mount-u može da ostavi container na starom inode-u; `urep doctor` upozorava, preporuka je directory mount.

**Primer:**

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

## 5. Discovery i membership

1. **ANNOUNCE** (UDP multicast, 5 s): `{node_id, tags, role, tcp_addr, proto, color, last_path, last_ts}`. AEAD token-ključem.
2. **Seeds** (opciono): direktan TCP connect.
3. **Gossip**: `HELLO` nosi listu poznatih peer-ova sa tagovima/statusom.
4. **Liveness**: `up` → `suspect` → `down` (3 promašaja / 45 s).

`slave` se announce-uje isto, da bi ga peer-ovi videli.

## 6. Status

`GREEN` · `YELLOW` (hook pao, konflikt, preskočen symlink, non-atomic write) · `RED` (apply/pre_hook pao, NACK, clock skew) · `PAUSED`.

```json
{"color":"GREEN","role":"peer","last_path":"/urep/app.yaml","last_peer":"web-02",
 "last_action":"update","last_hook_exit":0,"last_ms":41,"last_ts":"2026-11-04T16:22:03Z",
 "conflicts":0}
```

Status ide u ANNOUNCE i PONG. `YELLOW` zbog konflikta se čisti prvim uspešnim apply-em.

## 7. Protokol

TCP, frame `[4B len][1B type][12B nonce][AEAD ciphertext]`. Max frame 1 MiB.

| Poruka | Sadržaj |
|---|---|
| `HELLO` / `HELLO_ACK` | proto, node_id, tags, role, nonce, HMAC challenge |
| `PING` / `PONG` | seq, poslato_at, status, RTT |
| `MANIFEST_REQ` / `MANIFEST` | batch od 1000 zapisa `{path, h, v, m, d}` |
| `FILE_PUT` | path, version, blob, mode, size, tags, pre_hook, post_hook, content |
| `FILE_CHUNK` | transfer_id, seq, data (256 KiB) |
| `FILE_DELETE` | path, version, tags, pre_hook, post_hook |
| `FILE_GET` / `FILE_DATA` | blob (opciona optimizacija) |
| `ACK` / `NACK` | msg_id, status, reason |
| `PEERS` | lista nodova + tags/role/status |

- Handshake: token se nikad ne šalje; HMAC challenge.
- Ključevi: `HKDF-SHA256(token, salt=cluster_id) → enc_key, mac_key`.
- AAD uključuje `node_id` i tip poruke.
- `msg_id = SHA-256(tip ‖ path ‖ version ‖ content_hash)`; u commit traileru.
- `content_hash` = git blob OID (SHA-256 repo).
- `NACK`: `tag_mismatch`, `path_not_allowed`, `path_forbidden`, `bad_hash`, `paused`, `pre_hook_failed`, `slave_readonly`.

## 8. Git kao jedini izvor stanja

Nema indeksa ni keša na disku. Stanje je funkcija git istorije:

```
state(path) = poslednji commit koji je dirao path
```

### 8.1 Format commita

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

Polja: `p` putanja u ogledalu, `o` operacija (`A`/`M`/`D`), `m` mode, `v` HLC, `hp`/`ho` hook komande.

### 8.2 Derivacija pri startu

```
state = {}                                  # path → {h, v, m, d, hp, ho, commit, ts}

for commit in git log --raw -z --format=…   # newest-first
    data = json(Urep-Data trailer)
    for c in data.changes:
        if c.p not in state:                # prvi (najnoviji) commit pobeđuje
            state[c.p] = {h: blob_from_raw(c), v: c.v, m: c.m,
                          d: (c.o == "D"), hp: c.hp, ho: c.ho,
                          commit: sha, ts: time}
```

- Jedan prolaz kroz istoriju; `git log --raw` daje blob OID i status.
- Tombstone: poslednji commit za putanju je `D`.
- Rezultat je in-memory mapa koja se održava inkrementalno pri svakom novom commitu.
- **Startup je O(broj commit-a)**; raste linearno sa istorijom.

### 8.3 Konzistentnost

- Za svaki `d=false` blob mora da postoji (`git cat-file -e`); `urep doctor` verifikuje.
- Fajl na disku koji nije u `state` → na `peer` nodu watcher ga commit-uje; na `slave` nodu `urep doctor` prijavljuje drift.
- Nema `urep index rebuild` — svaki start je rebuild.
- `SIGHUP` ponovo izvodi stanje iz git-a i re-attach-uje watcher-e.
- Manifest za anti-entropy se gradi iz in-memory mape.

### 8.4 Istorija

- Commit nastaje pre `post_hook`-a; hook rezultat ide u `events.jsonl` i status.
- Istorija se čuva zauvek; `git gc --auto` održava repo.

## 9. Replikacija: lokalna izmena (samo `peer`)

1. Watcher prati parent direktorijum svake `UREP_PATH` i filtrira tačan filename (`IN_MODIFY`, `IN_MOVED_TO`, `IN_CREATE`, `IN_DELETE`); parent se kreira ako ne postoji; re-attach ako nestane.
2. Debounce `UREP_DEBOUNCE` (200 ms), batch do 2 s.
3. Re-hash celog fajla (editori rade temp+rename).
4. Hash jednak in-memory stanju → ignoriši (echo suppression).
5. Upis u ogledalo + commit + update in-memory mape.
6. `FILE_PUT` peer-ovima čiji tagovi odgovaraju.

Lokalne izmene ne pokreću hooks. Symlink se preskače + warning.

## 10. Apply: primljena izmena

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
      snapshot: commit lokalnog stanja kao "urep: pre-join snapshot"

  if local and remote.version <= local.version:   return ACK(noop)
  if local and |remote.wall - local.wall| < conflict_window and content differs:
      mark YELLOW(conflict) + conflict ref

  os.makedirs(parent, dir_mode, exist_ok=True)
  pre_hook (jednom po batch-u) || return NACK(pre_hook_failed) + RED
  write (4.5: replace ili in-place fallback) + chmod
  mirror write + commit(remote atribucija) + update in-memory mape
  post_hook (jednom po batch-u) → GREEN/YELLOW
```

- LWW: HLC trojke `(wall_ms, counter, node_id)` leksikografski.
- Jedan lock po putanji; hook-ovi se nikad ne preklapaju.
- Env za hooks: `UREP_PATH`, `UREP_ACTION`, `UREP_PEER`, `UREP_VERSION`, `UREP_MODE`.

## 11. Brisanje

- Lokalni `IN_DELETE`/`IN_MOVED_FROM` (samo `peer`) → commit `D` + nova HLC.
- `FILE_DELETE` → ako je verzija novija, `os.unlink` + commit `D` (tombstone u istoriji).
- Hooks se izvršavaju i na brisanje (`UREP_ACTION=delete`).
- Tombstone živi u istoriji zauvek.

## 12. Konflikti

- Pobednik je veći HLC — deterministički.
- Ako su verzije bliže od `UREP_CONFLICT_WINDOW` (2 s) i sadržaj se razlikuje → `YELLOW` + ref:

```
git update-ref refs/urep/conflict/<hexpath>/<hlc> <losing_commit>
```

- `urep conflicts` / `urep restore <ref>`.
- `YELLOW` se čisti prvim uspešnim apply-em.

## 13. Anti-entropy i join

- Na connect i periodično (`UREP_ANTI_ENTROPY`, 60 s): `MANIFEST` batchevi iz in-memory mape.
- Diff po `(path, h, v)` → novija strana šalje `FILE_PUT` sa hooks iz mape.
- Primenjena remote izmena se prosleđuje dalje **osim pošiljaocu**, da bi stigla i do nodova koji nisu direktno povezani.
- Fajlovi >900 KB idu kroz `FILE_CHUNK` (256 KB); gornja granica je `UREP_MAX_FILE_SIZE`.
- Novi nod: lokalni fajl se snapshot-uje, pa klaster pobeđuje.
- `slave` sme `MANIFEST_REQ` + prijem; nikad ne šalje fajlove.
- Hooks se izvršavaju i pri catch-up-u; `urep sync --no-hooks` preskače hooks za jednu sinkronizaciju.
- Manifest za 10k fajlova ~1 MB → batch 200.

## 14. Pause / resume

```
urep pause [--in|--out] [--for 10m]
urep resume
```

- Default: pauzira oba smera.
- Watcher i commit nastavljaju (na `peer` nodu) — izmene se beleže, ne šalju.
- Peer dobija `NACK paused`; markira `PAUSED`.
- Stanje u `state.json` (preživljava restart).
- `resume` pokreće anti-entropy — nema reda čekanja.
- Discovery/ping ostaju aktivni.

## 15. CLI

```
urep [--token T] [--prefix P] [--tag T]... [--path P]... [--root R]...
     [--slave] [--listen ADDR] [--pre-hook CMD] [--post-hook CMD]
     # bez podkomande = daemon

urep init [--token-file PATH] [--print-token]
urep id [--prefix P]              # generiše node ID bez pokretanja daemona
urep ping HOST:PORT               # handshake + PING/PONG
urep daemon [-f] [--slave]
urep status [--json]
urep peers [--watch]
urep config [--json]              # efektivna konfiguracija, token redigovan
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

- `urep` bez podkomande = `urep daemon` (container entrypoint).
- `--token` je jedini obavezan flag; alternative `UREP_TOKEN`, `UREP_TOKEN_FILE`, `--token-file`.
- Node ID je disposable UUID po startu; `--prefix` samo dodaje čitljiv prefiks.
- Nema `path add/remove`, nema `root add/remove`, nema `--config` — konfiguracija je env/CLI, menja se restartom.
- CLI ↔ daemon preko `run/urep.sock`. `SIGHUP` re-derive, `SIGTERM` graceful.

## 16. Bezbednost

- Token: 0600, nikad u logu. U containeru preferirati `UREP_TOKEN_FILE`.
- Enkripcija svih poruka, uključujući discovery.
- Write allowlist: `UREP_ACCEPT_ROOTS` + `realpath(parent)` + deny lista.
- Tag provera na obe strane.
- Hooks = RCE po dizajnu (prihvaćeno). Mitigacije:
  - komanda je vezana za izmenu fajla — nema „EXEC“ poruke;
  - `UREP_HOOKS_ENABLED=false`;
  - timeout + max dužina;
  - audit u `events.jsonl`.
- Container može da radi kao non-root ako piše samo u `/urep` i ne koristi hooks.

## 17. Fajl sistem

- Samo eksplicitno navedeni fajlovi; nema rekurzije, globova, auto-scan-a.
- Watcher prati parent direktorijum i filtrira tačan filename; parent se kreira.
- Symlink: `lstat` → preskoči + warning.
- Specijalni fajlovi: preskoči + warning.
- `fs.inotify.max_user_watches`; `urep doctor` proverava.
- Metadata: sadržaj + mode. Vlasnik ostaje lokalni.

## 18. Ograničenja i performanse

- Fajl ≤ 16 MiB, chunk 256 KiB, frame ≤ 1 MiB.
- Cilj: < 500 ms LAN latencija, stotine nodova, hiljade fajlova.
- Rate limit 50 poruka/s po peer-u; jedan apply po putanji.
- Startup je O(broj commit-a) zbog derivacije; `urep doctor` meri vreme.
- Clock skew > `UREP_MAX_CLOCK_SKEW` (5 min): `RED`, odbija apply, loguje.
- HLC nikad ne ide unazad: `wall_ms = max(now, last_wall)`.

## 19. Stack

Python 3.11+ · `asyncio` · `inotify_simple` · `cryptography` · `cbor2` · `subprocess` + sistemski `git ≥ 2.29` (SHA-256 repo) · `pydantic` (validacija env-a) · `typer` · `structlog` · `pytest` + `hypothesis` · `uv` · Docker.

**Sav razvoj i testiranje ide kroz Docker** — host Python se ne koristi (vidi 25).

## 20. Testiranje

**Sva testiranja se izvršavaju u Dockeru.** `pytest` na hostu nije podržan; host sme samo `docker` i `make`.

- **Unit:** HLC, LWW, derivacija stanja iz sintetičke istorije, frame codec, tag matching, hook timeout, env precedence, UUID node ID.
- **Integration:** 3-nod klaster; tag filtriranje; `slave` prima ali ne šalje; delete → tombstone iz istorije; novi nod → snapshot + backfill; parent dir se kreira.
- **Container:** ENV-only start; `-v file:file` → in-place fallback; `-v dir:/urep` → atomic write; restart sa state volume.
- **Chaos:** kill -9 u sred apply-a (derivacija mora da vrati konzistentno stanje), particija, clock skew, dupli paketi. Zahteva `NET_ADMIN`, markirano `-m chaos`.
- **Fuzz:** frame decoder, malformisan CBOR, predugačke putanje, malformisan `Urep-Data` JSON.
- **Security:** nevažeći token, replay, path traversal, `accept_roots` escape, tag mismatch.

Komande: `make test`, `make test-unit`, `make test-integration`, `make test-chaos`, `make lint`, `make build`. CI koristi isti `Dockerfile.dev` — nema `setup-python` na hostu.

## 21. Faze

| Faza | Sadržaj |
|---|---|
| M0 | **Docker dev image (Python + git + test alati), compose harness, `make` targeti, CI** |
| M1 | skeleton, env/CLI config, git repo, derivacija stanja iz istorije, HLC, watcher + commit |
| M2 | transport + handshake + AEAD |
| M3 | multicast discovery + tags/role + gossip + status/ping |
| M4 | FILE_PUT/apply, tag match, accept_roots, LWW, atomic write + fallback, ACK/NACK |
| M5 | hooks (globalni, batch koalescencija), events, status boje, delete hooks |
| M6 | tombstone, konflikti + YELLOW, anti-entropy, join/backfill |
| M7 | `--slave`, pause/resume, CLI, Docker image, systemd |
| M8 | chaos/fuzz, hardening, dokumentacija |

## 22. Otvorena pitanja (za v2)

Key rotation · relay za NAT · bandwidth limit · uid/gid/xattr · symlink replikacija · direktorijumi · per-path tagovi/hooks · runtime izmena konfiguracije · TUI dashboard · `urep gc` politika za tombstone · Kubernetes manifest · opcioni keš stanja ako istorija postane prevelika.

---

## 23. Changelog odluka

**v1.0 → v1.1:** symlink u v1, prvi join klaster pobeđuje, hooks na catch-up/delete, koalescencija, konflikti + YELLOW, drop-in config, argv+shell hooks.

**v1.1 → v1.2:** tagovi kao pretplata, `accept_roots`, `--slave`, symlink i direktorijumi van v1, parent dir se kreira.

**v1.2 → v1.3:** ENV mode bez TOML-a, `--token` jedini obavezan flag, default tag `default`, nema auto-scan-a, container-first, single-file mount fallback.

**v1.3 → v1.4:** `versions.json` uklonjen; stanje se izvodi iz git istorije; `Urep-Data` JSON trailer.

**v1.4 → v1.5:**
- **Config fajlovi potpuno uklonjeni.** Nema `urep.toml`, nema `paths.d/`, nema `--config`.
- Konfiguracija isključivo CLI + `UREP_*` env; precedence CLI > ENV > default.
- `urep path add/remove` i `urep root add/remove` uklonjeni — izmene idu restartom.
- Tagovi postaju globalni za sve putanje; hooks takođe globalni (`UREP_PRE_HOOK`/`UREP_POST_HOOK`), sa `UREP_PATH` u env-u za grananje.
- Dodat `urep config` za prikaz efektivne konfiguracije.
- Preporučeni pattern: jedan kontejner = jedan fajl = jedan hook.

**v1.5 → v1.6:** node ID je disposable UUID po startu (ne čuva se), `--prefix` za čitljivost, `urep id` generator.

**v1.6 → v1.7:**
- **Sav razvoj i testiranje isključivo u Dockeru**; host Python nije podržan.
- Dodati `Dockerfile` (runtime: Python + git) i `Dockerfile.dev` (Python + git + test alati).
- `docker-compose.dev.yml` / `docker-compose.test.yml`, `make` targeti, CI bez `setup-python`.
- Multicast napomena: Docker bridge ne prosleđuje multicast; integracija koristi `UREP_SEEDS`, `-m multicast` ide na host mrežu.
- M0 dodat kao obavezan prvi korak.

**v1.7 → v1.8 (implementirano M2–M4):**
- **M2:** token handshake (HMAC challenge, token se ne šalje), HKDF + ChaCha20-Poly1305 frame enkripcija, TCP transport, `urep ping`.
- **M3:** UDP multicast discovery sa AEAD-om, gossip, tagovi/role, `peers.json`, `urep peers [--watch]`.
- **M4:** `FILE_PUT` / `FILE_DELETE`, chunking, LWW apply, `accept_roots`, tag provera, pre/post hooks na receiveru, tombstone brisanje.
- Manifest anti-entropy na connect i periodično; primenjene izmene se prosleđuju dalje kroz mesh (osim pošiljaocu).
- `urep status` boja se izvodi iz poslednjeg eventa (`GREEN`/`YELLOW`/`RED`/`PAUSED`).
- Hook-ovi su još per-file; batch koalescencija ostaje za M5.

## 24. Napomene pre razvoja

1. **Git je tvrda zavisnost** — `urep doctor` proverava `git >= 2.29` i `--object-format=sha256`.
2. **Hooks kao root RCE** je svesna odluka; `UREP_HOOKS_ENABLED=false` je jedan potez za gašenje.
3. **`UREP_ACCEPT_ROOTS` je jedina write granica** — preširok root znači da token holder može da piše svuda pod njim.
4. **Single-file mount nije atomičan** — preporuka je directory mount.
5. **Startup raste sa istorijom** — ako postane problem, v2 može da doda opcioni keš.
6. **Jedan kontejner = jedan fajl** je najčistiji model; više fajlova deli iste tagove i hook-ove.
7. **Node ID je disposable** — posle restarta nod izgleda kao novi; nema načina da se fiksira.
8. **Host Python nije podržan** — sve ide kroz Docker; `make test` je jedini ulaz.

---

## 25. Razvoj i testiranje isključivo u Dockeru

**Pravilo:** nijedan korak razvoja, testiranja ili build-a se ne izvršava na hostu. Nema host `python`, nema host `.venv`, nema `pytest` van kontejnera. Host sme samo `docker` i `make`.

### 25.1 Image-i

- **`Dockerfile` (runtime):** `python:3.12-slim-bookworm` + `git` + `ca-certificates`; multi-stage; `uv sync --frozen --no-dev`; `ENTRYPOINT ["urep"]`; `STOPSIGNAL SIGTERM`; volumes `/urep`, `/var/lib/urep`; `git >= 2.29` (Debian bookworm ima 2.39).
- **`Dockerfile.dev` (dev/test):** isti base + `uv`, `pytest`, `ruff`, `mypy`, `iproute2`, `procps`, `iptables`; `UV_PROJECT_ENVIRONMENT=/opt/venv` (da bind mount ne prekrije venv); default `CMD ["pytest"]`.
- **`docker-compose.dev.yml`:** `dev` servis sa bind mountom repo-a (`.:/src`) i state volume-om.
- **`docker-compose.test.yml`:** `node-a`, `node-b`, `node-c`, `test` na istoj mreži.

### 25.2 Mreža u testovima

Docker bridge **ne prosleđuje multicast**. Zato:

- integracioni testovi koriste `UREP_SEEDS` (deterministički), ne multicast;
- poseban `-m multicast` test koristi `network_mode: host` (Linux) ili macvlan i opt-in je;
- chaos testovi (particija) traže `cap_add: [NET_ADMIN]` i markirani su `-m chaos`.

### 25.3 Komande

```
make dev                 # bash u dev kontejneru
make test                # svi testovi u Dockeru
make test-unit
make test-integration
make test-chaos
make lint                # ruff + mypy
make build               # runtime image
make smoke               # pokreni image i proveri `urep doctor`
```

Svi targeti su tanki wrap-eri oko `docker compose run --rm` / `docker build`.

### 25.4 CI

- Jedan job: `docker build -f Dockerfile.dev -t urep-dev .` → `docker run --rm urep-dev pytest`.
- Build runtime image i smoke test: `docker run --rm urep:ci --token test --path /tmp/x` + `urep doctor`.
- Nema `actions/setup-python`; nema host dependency install-a.

### 25.5 Kriteriji prihvatanja

- `docker build -f Dockerfile.dev .` prolazi bez ikakvih host zavisnosti osim Docker-a.
- `make test` prolazi na čistom hostu.
- Runtime image sadrži `git >= 2.29` i podržava `--object-format=sha256`.
- Integration testovi podižu 3 noda i dokazuju konvergenciju.
- Nijedan test ne zahteva host Python ni host `pytest`.
