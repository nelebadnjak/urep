#!/usr/bin/env bash
set -euo pipefail

IMAGE="${UREP_IMAGE:-urep:latest}"
TOKEN="${UREP_TOKEN:-test-token}"
TAG="${UREP_TAG:-test}"

cleanup() {
  docker rm -f urep-a urep-b >/dev/null 2>&1 || true
  rm -rf /tmp/urep-a /tmp/urep-b /tmp/urep-a-state /tmp/urep-b-state
}

if [[ "${1:-}" == "--cleanup" ]]; then
  cleanup
  echo "cleaned"
  exit 0
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "docker not found" >&2
  exit 1
fi

if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  echo "image $IMAGE not found; run: make build" >&2
  exit 1
fi

cleanup
mkdir -p /tmp/urep-a /tmp/urep-b /tmp/urep-a-state /tmp/urep-b-state
printf 'hello\n' | tee /tmp/urep-a/app.yaml > /tmp/urep-b/app.yaml

docker run -d --name urep-a --network host --user "$(id -u):$(id -g)" \
  -e HOME=/tmp \
  -e UREP_TOKEN="$TOKEN" -e UREP_TAG="$TAG" \
  -e UREP_PATH=/urep/app.yaml -e UREP_LISTEN=0.0.0.0:7801 \
  -e UREP_SEEDS=127.0.0.1:7802 -e UREP_MULTICAST=false \
  -e UREP_STATE_DIR=/var/lib/urep \
  -v /tmp/urep-a:/urep -v /tmp/urep-a-state:/var/lib/urep \
  "$IMAGE" >/dev/null

docker run -d --name urep-b --network host --user "$(id -u):$(id -g)" \
  -e HOME=/tmp \
  -e UREP_TOKEN="$TOKEN" -e UREP_TAG="$TAG" \
  -e UREP_PATH=/urep/app.yaml -e UREP_LISTEN=0.0.0.0:7802 \
  -e UREP_SEEDS=127.0.0.1:7801 -e UREP_MULTICAST=false \
  -e UREP_STATE_DIR=/var/lib/urep \
  -v /tmp/urep-b:/urep -v /tmp/urep-b-state:/var/lib/urep \
  "$IMAGE" >/dev/null

# Sačekaj da se peer veza uspostavi.
for _ in $(seq 1 30); do
  if docker exec urep-b urep peers 2>/dev/null | grep -q 'peer'; then
    break
  fi
  sleep 0.5
done

printf 'changed-by-a\n' > /tmp/urep-a/app.yaml

# Sačekaj da B primi izmenu.
for _ in $(seq 1 30); do
  if grep -q 'changed-by-a' /tmp/urep-b/app.yaml 2>/dev/null; then
    break
  fi
  sleep 0.5
done

echo "--- B file ---"
cat /tmp/urep-b/app.yaml
echo "--- B status ---"
docker exec urep-b urep status
echo "--- B peers ---"
docker exec urep-b urep peers
echo "--- B log ---"
docker exec urep-b urep log

echo
echo "cleanup: ./test.sh --cleanup"
