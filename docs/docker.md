# uRep — Docker razvoj i testiranje

Deo plana v1.7. **Pravilo: nijedan korak razvoja, testiranja ili build-a se ne izvršava na hostu.**
Host sme samo `docker` i `make`; nema host `python`, nema host `.venv`, nema host `pytest`.

---

## 1. Fajlovi

```
.
├── Dockerfile                 # runtime image (Python + git)
├── Dockerfile.dev             # dev/test image (Python + git + pytest/ruff/mypy)
├── docker-compose.dev.yml     # interaktivni dev
├── docker-compose.test.yml    # 3-nod integration + test runner
├── Makefile                   # jedini ulaz: make test / make dev / make build
├── .dockerignore
├── pyproject.toml
├── uv.lock
└── src/urep/
```

---

## 2. Runtime image

`python:3.12-slim-bookworm` + `git` (bookworm ima git 2.39 → SHA-256 repo podržan).

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

Napomena: image je root po defaultu jer hooks i `/etc` upisi to traže. Ako se koristi samo `/urep`, može `--user 10001:10001` uz odgovarajuće permisije volume-a.

Build:

```bash
docker build -t urep:dev .
docker run --rm urep:dev version
docker run --rm urep:dev doctor      # proverava git >= 2.29 + sha256
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

# venv van /src da ga bind mount ne prekrije
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    PYTHONPATH=/src/src \
    PYTHONUNBUFFERED=1

WORKDIR /src
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project

CMD ["pytest"]
```

Test markeri u `pyproject.toml`:

```toml
[tool.pytest.ini_options]
markers = [
  "unit: brzi testovi bez mreže",
  "integration: visenodni klaster u Dockeru",
  "chaos: particija / kill -9 / clock skew (NET_ADMIN)",
  "multicast: zahteva host mrežu",
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
# u kontejneru:
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

### Multicast napomena

Docker bridge **ne prosleđuje multicast**. Zato:

- integracioni testovi koriste `UREP_SEEDS` (deterministički);
- `-m multicast` test ide sa `network_mode: host` (Linux) ili macvlan i opt-in je;
- `-m chaos` testovi traže `cap_add: [NET_ADMIN]` (iptables particija) i `kill -9` na container proces.

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

Nema `actions/setup-python`; nema instalacije zavisnosti na hostu.

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

## 9. Kriteriji prihvatanja

- [ ] `docker build -f Dockerfile.dev .` prolazi na hostu gde postoji samo Docker.
- [ ] `make test` prolazi bez host Python-a.
- [ ] Runtime image sadrži `git >= 2.29` i `urep doctor` potvrđuje SHA-256 repo.
- [ ] `make test-integration` podiže 3 noda i dokazuje konvergenciju.
- [ ] `make test-chaos` prolazi sa `NET_ADMIN`.
- [ ] `make smoke` pokreće runtime image i `urep doctor` je zelen.
- [ ] Nijedan dokumentovan korak ne pominje host `pip`, `venv` ili `pytest`.

---

## 10. Napomene

- Multi-arch build (opciono): `docker buildx build --platform linux/amd64,linux/arm64`.
- Runtime image je ~150–200 MB (`python:3.12-slim` + git).
- Ako je potreban non-root: `--user 10001:10001` i volume-i sa odgovarajućim vlasništvom; hooks tada ne mogu da rade stvari koje traže root.
- `uv.lock` je obavezan u buildu (`--frozen`); bez njega build pada.
