# uRep

**micro replication** — decentralizovani P2P sync agent za konfiguracione fajlove.
Bez lidera, bez baze; lokalni git je jedini izvor stanja.

## Development

Sve ide kroz Docker. Host sme samo `docker` i `make`.

```bash
make dev      # bash u dev kontejneru
make test     # svi testovi u Dockeru
make lint     # ruff + mypy u Dockeru
make build    # runtime image
make smoke    # pokreni runtime image
```

## Run

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

Bez podkomande `urep` pokreće daemon.

## Status (M0–M4)

Implementirano:

- Docker dev/runtime image, compose harness, `make` targeti, CI bez host Python-a
- env/CLI konfiguracija (`UREP_*` + flagovi), HLC, git ogledalo (SHA-256) i derivacija stanja iz istorije
- watcher pojedinačnih fajlova + commit, atomic write sa `EBUSY` fallback-om
- token handshake bez slanja tokena, HKDF + ChaCha20-Poly1305 frame enkripcija
- UDP multicast discovery + gossip, tagovi/role, `peers.json`
- manifest anti-entropy, LWW apply, `accept_roots`, tag provera, tombstone brisanje
- pre/post hooks na receiveru (update i delete), `events.jsonl` audit
- chunked transfer za fajlove veće od 900 KB (do `UREP_MAX_FILE_SIZE`)
- forwarding primenjenih izmena dalje kroz mesh (exclude sender) + periodični manifest
- CLI: `daemon`, `status`, `config`, `doctor`, `id`, `ping`, `peers`, `pause`, `resume`, `log`, `show`, `diff`, `revert`, `conflicts`, `restore`, `events`, `gc`, `version`

Napomene:

- Docker bridge ne prosleđuje multicast; u testovima i na bridge mrežama koristi `UREP_SEEDS`.
- Hook-ovi se za sada izvršavaju po fajlu; batch koalescencija je sledeći korak.
- `urep sync` je još stub (kontrolni socket dolazi u M7).

Sledeće: M5 hooks polish/grupisanje, M6 konflikti/anti-entropy detalji, M7 kontrolni socket/systemd.
