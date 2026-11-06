# uRep — Docker development and testing

Part of the v1.7 plan. **Rule: no development, testing, or build step runs on the host.**
The host may only use `docker` and `make`; no host `python`, no host `.venv`, no host `pytest`.

---

## 1. Files

```
.
├── Dockerfile                 # runtime image (Python + git)
├── Dockerfile.dev             # dev/test image (Python + git + pytest/ruff/mypy)
├── docker-compose.dev.yml     # interactive dev
├── docker-compose.test.yml    # 3-node integration + test runner
├── Makefile                   # single entry point: make test / make dev / make build
├── .dockerignore
├── pyproject.toml
├── uv.lock
└── src/urep/
```

---

## 2. Runtime image

`python:3.12-slim-bookworm` + `git` (bookworm has git 2.39 → SHA-256 repos supported).

```dockerfile
# syntax=docker/dockerfile:1
FROM python:3.12-slim-bookworm AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

WORKDIR /src
COPY pyproject.toml uv.lock ./
COPY src ./src
RUN uv sync --frozen --no-dev


FROM python:3.12-slim-bookworm AS runtime

RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY --from=builder /src/.venv /app/.venv
COPY --from=builder /src/src /app/src
COPY pyproject.toml uv.lock /app/

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UREP_STATE_DIR=/var/lib/urep \
    UREP_DATA_DIR=/urep

VOLUME ["/urep", "/var/lib/urep"]
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=15s --timeout=3s --retries=3 \
  CMD urep status --json >/dev/null || exit 1

ENTRYPOINT ["urep"]
CMD ["daemon"]
```

Note: the image runs as root by default because hooks and `/etc` writes require it. If only `/urep` is used, `--user 10001:10001` works with appropriate volume permissions.

Build:

```bash
docker build -t urep:dev .
docker run --rm urep:dev version
docker run --rm urep:dev doctor      # checks git >= 2.29 + sha256
```

---

## 3. Dev/test image

```dockerfile
# syntax=docker/dockerfile:1
FROM python:3.12-slim-bookworm

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      git ca-certificates curl iproute2 procps iptables \
 && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

# venv outside /src so the bind mount does not hide it
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    PYTHONPATH=/src/src \
    PYTHONUNBUFFERED=1

WORKDIR /src
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project

CMD ["pytest"]
```

Test markers in `pyproject.toml`:

```toml
[tool.pytest.ini_options]
markers = [
  "unit: fast tests without network",
  "integration: multi-node cluster in Docker",
  "chaos: partition / kill -9 / clock skew (NET_ADMIN)",
  "multicast: requires host network",
]
```

---

## 4. `docker-compose.dev.yml`

```yaml
services:
  dev:
    build:
      context: .
      dockerfile: Dockerfile.dev
    image: urep:dev
    working_dir: /src
    volumes:
      - .:/src
      - urep-dev-state:/var/lib/urep
    environment:
      UREP_TOKEN: dev-token
      UREP_TAG: dev
      UREP_STATE_DIR: /var/lib/urep
    command: ["bash"]

volumes:
  urep-dev-state:
```

```bash
docker compose -f docker-compose.dev.yml run --rm dev bash
# inside the container:
pytest -m unit
ruff check .
mypy src
```

---

## 5. `docker-compose.test.yml`

```yaml
services:
  node-a:
    build: {context: ., dockerfile: Dockerfile}
    image: urep:test
    environment:
      UREP_TOKEN: test-token
      UREP_TAG: test
      UREP_PREFIX: node-a
      UREP_PATH: /urep/file.yaml
      UREP_SEEDS: node-b:7801,node-c:7801
      UREP_STATE_DIR: /var/lib/urep
    volumes:
      - ./tests/fixtures/node-a:/urep
    healthcheck:
      test: ["CMD", "urep", "status", "--json"]
      interval: 2s
      timeout: 2s
      retries: 30

  node-b:
    image: urep:test
    environment:
      UREP_TOKEN: test-token
      UREP_TAG: test
      UREP_PREFIX: node-b
      UREP_PATH: /urep/file.yaml
      UREP_SEEDS: node-a:7801,node-c:7801
      UREP_STATE_DIR: /var/lib/urep
    volumes:
      - ./tests/fixtures/node-b:/urep
    healthcheck:
      test: ["CMD", "urep", "status", "--json"]
      interval: 2s
      timeout: 2s
      retries: 30

  node-c:
    image: urep:test
    environment:
      UREP_TOKEN: test-token
      UREP_TAG: test
      UREP_PREFIX: node-c
      UREP_ROLE: slave
      UREP_STATE_DIR: /var/lib/urep
    volumes:
      - ./tests/fixtures/node-c:/urep
    healthcheck:
      test: ["CMD", "urep", "status", "--json"]
      interval: 2s
      timeout: 2s
      retries: 30

  test:
    build: {context: ., dockerfile: Dockerfile.dev}
    image: urep:dev
    depends_on:
      node-a: {condition: service_healthy}
      node-b: {condition: service_healthy}
      node-c: {condition: service_healthy}
    working_dir: /src
    volumes:
      - .:/src
    environment:
      UREP_TEST_TOKEN: test-token
    command: ["pytest", "-m", "integration"]

networks:
  default:
    name: urep-test
```

### Multicast note

Docker bridge **does not forward multicast**. Therefore:

- integration tests use `UREP_SEEDS` (deterministic);
- the `-m multicast` test runs with `network_mode: host` (Linux) or macvlan and is opt-in;
- `-m chaos` tests require `cap_add: [NET_ADMIN]` (iptables partition) and `kill -9` on the container process.

---

## 6. `Makefile`

```make
SHELL := /bin/bash
COMPOSE_DEV  := docker compose -f docker-compose.dev.yml
COMPOSE_TEST := docker compose -f docker-compose.test.yml

.PHONY: dev test test-unit test-integration test-chaos lint build smoke clean

dev:
	$(COMPOSE_DEV) run --rm dev bash

test:
	$(COMPOSE_TEST) run --rm test pytest

test-unit:
	$(COMPOSE_DEV) run --rm dev pytest -m unit

test-integration:
	$(COMPOSE_TEST) build node-a node-b node-c test
	$(COMPOSE_TEST) run --rm test pytest -m integration

test-chaos:
	$(COMPOSE_TEST) run --rm test pytest -m chaos

lint:
	$(COMPOSE_DEV) run --rm dev sh -c "ruff check . && mypy src"

build:
	docker build -t urep:dev .

smoke: build
	docker run --rm urep:dev --token test --path /tmp/x --prefix smoke
	docker run --rm urep:dev doctor

clean:
	$(COMPOSE_DEV) down -v
	$(COMPOSE_TEST) down -v
```

---

## 7. CI (GitHub Actions)

```yaml
name: ci
on: [push, pull_request]

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: make build
      - run: make test
      - run: make smoke
```

No `actions/setup-python`; no dependency installation on the host.

---

## 8. `.dockerignore`

```
.git
.venv
__pycache__
*.pyc
.pytest_cache
.mypy_cache
.ruff_cache
dist
build
```

---

## 9. Acceptance criteria

- [ ] `docker build -f Dockerfile.dev .` passes on a host that only has Docker.
- [ ] `make test` passes without host Python.
- [ ] The runtime image contains `git >= 2.29` and `urep doctor` confirms the SHA-256 repo.
- [ ] `make test-integration` brings up 3 nodes and proves convergence.
- [ ] `make test-chaos` passes with `NET_ADMIN`.
- [ ] `make smoke` runs the runtime image and `urep doctor` is green.
- [ ] No documented step mentions host `pip`, `venv`, or `pytest`.

---

## 10. Notes

- Multi-arch build (optional): `docker buildx build --platform linux/amd64,linux/arm64`.
- The runtime image is ~150–200 MB (`python:3.12-slim` + git).
- If non-root is required: `--user 10001:10001` and volumes with appropriate ownership; hooks then cannot do things that require root.
- `uv.lock` is required in the build (`--frozen`); without it the build fails.
