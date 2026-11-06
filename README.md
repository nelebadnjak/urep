# uRep

**micro replication** — a decentralized P2P sync agent for configuration files.
No leader, no database; a local git repository is the only source of state.

## Development

Everything runs through Docker. The host may only use `docker` and `make`.

```bash
make dev      # bash in the dev container
make test     # all tests in Docker
make lint     # ruff + mypy in Docker
make build    # runtime image
make smoke    # run the runtime image
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

Without a subcommand, `urep` starts the daemon.

## Status (M0–M4)

Implemented:

- Docker dev/runtime images, compose harness, `make` targets, CI without host Python
- env/CLI configuration (`UREP_*` + flags), HLC, git mirror (SHA-256), state derivation from history
- per-file watcher + commit, atomic write with `EBUSY` fallback
- token handshake without sending the token, HKDF + ChaCha20-Poly1305 frame encryption
- UDP multicast discovery + gossip, tags/roles, `peers.json`
- manifest anti-entropy, LWW apply, `accept_roots`, tag checks, tombstone deletes
- pre/post hooks on the receiver (update and delete), `events.jsonl` audit
- chunked transfer for files larger than 900 KB (up to `UREP_MAX_FILE_SIZE`)
- forwarding of applied changes through the mesh (excluding the sender) + periodic manifest
- CLI: `daemon`, `status`, `config`, `doctor`, `id`, `ping`, `peers`, `pause`, `resume`, `log`, `show`, `diff`, `revert`, `conflicts`, `restore`, `events`, `gc`, `version`

Notes:

- Docker bridge does not forward multicast; in tests and on bridge networks use `UREP_SEEDS`.
- Hooks currently run per file; batch coalescing is the next step.
- `urep sync` is still a stub (control socket arrives in M7).

Next: M5 hook polish/batching, M6 conflict/anti-entropy details, M7 control socket/systemd.
