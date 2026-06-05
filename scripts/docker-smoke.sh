#!/usr/bin/env bash
# Build the telegram-assistant container, start it with a throwaway
# data/ volume, and verify GET /health returns 200.
#
# Usage: scripts/docker-smoke.sh
#
# The script is self-contained: it generates a temporary data/ directory with
# a minimal config.yml, builds the image, runs the container, polls /health,
# and cleans up regardless of the outcome.

set -euo pipefail

IMAGE_TAG="${IMAGE_TAG:-telegram-assistant:smoke}"
CONTAINER_NAME="${CONTAINER_NAME:-tpa-smoke-$$}"
HOST_PORT="${HOST_PORT:-18085}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA_DIR="$(mktemp -d -t tpa-smoke-XXXXXX)"

cleanup() {
    docker rm -f "${CONTAINER_NAME}" >/dev/null 2>&1 || true
    rm -rf "${DATA_DIR}"
}
trap cleanup EXIT

cat >"${DATA_DIR}/config.yml" <<'YAML'
telegram:
  api_id: 123456
  api_hash: "telegram_api_hash"
  session_path: /data/telegram-assistant.session
  main_account_label: planfix-assistant-main
  reserve_admins:
    - "@reserve_account"
  reserve_members:
    - "@planfix_bot"
  default_chat_folder:
    folder_id: 2
    folder_name: "Planfix clients"
  defaults:
    enable_topics: true
    create_invite_link: true

http:
  host: "0.0.0.0"
  port: 8085
  bearer_token: "secret_token"

queue:
  max_parallel_telegram_ops: 1
  default_retry_delay_seconds: 30
  flood_wait_safety_margin_seconds: 5

logging:
  level: INFO
YAML

echo ">>> building image ${IMAGE_TAG}"
docker build -t "${IMAGE_TAG}" "${REPO_ROOT}"

echo ">>> starting container ${CONTAINER_NAME} on port ${HOST_PORT}"
docker run -d \
    --name "${CONTAINER_NAME}" \
    -p "${HOST_PORT}:8085" \
    -v "${DATA_DIR}:/data" \
    "${IMAGE_TAG}" >/dev/null

echo ">>> waiting for /health to respond"
attempt=0
max_attempts=30
status=""
while [[ ${attempt} -lt ${max_attempts} ]]; do
    status="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${HOST_PORT}/health" || true)"
    if [[ "${status}" == "200" ]]; then
        break
    fi
    attempt=$((attempt + 1))
    sleep 1
done

if [[ "${status}" != "200" ]]; then
    echo "smoke test failed: /health returned '${status}' after ${max_attempts}s" >&2
    docker logs "${CONTAINER_NAME}" >&2 || true
    exit 1
fi

echo ">>> /health OK"
curl -s "http://127.0.0.1:${HOST_PORT}/health"
echo
