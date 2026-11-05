SHELL := /bin/bash
COMPOSE_DEV  := docker compose -f docker-compose.dev.yml
COMPOSE_TEST := docker compose -f docker-compose.test.yml

.PHONY: dev test test-unit test-integration test-chaos lint build smoke clean

dev:
	$(COMPOSE_DEV) run --rm dev bash

test:
	$(COMPOSE_DEV) run --rm dev pytest

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
	docker run --rm urep:dev version
	docker run --rm urep:dev doctor
	docker run --rm -e UREP_TOKEN=test -e UREP_PATH=/tmp/x -e UREP_ACCEPT_ROOTS=/tmp \
		urep:dev status --json

clean:
	$(COMPOSE_DEV) down -v
	$(COMPOSE_TEST) down -v
