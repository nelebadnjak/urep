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
  urep:dev
```

Bez podkomande `urep` pokreće daemon.

## Status (M1)

Implementirano:

- env/CLI konfiguracija (`UREP_*` + flagovi)
- HLC
- git ogledalo (SHA-256) + derivacija stanja iz istorije
- watcher pojedinačnih fajlova + commit
- CLI: `daemon`, `status`, `config`, `doctor`, `id`, `pause`, `resume`, `log`, `show`, `diff`, `revert`, `conflicts`, `restore`, `events`, `gc`, `version`

Sledeće: M2 transport + AEAD, M3 discovery, M4 apply/LWW, M5 hooks, M6 anti-entropy.
