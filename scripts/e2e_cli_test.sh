#!/usr/bin/env bash
# Secondary end-to-end script: exercises CLI surfaces against the same
# resources that scripts/e2e_test.sh creates over HTTP. Keeps coverage
# for the CLI subcommands defined in the plan:
#   * health, folders inspect, topics create + close, messages send,
#     members bulk-remove (--dry-run), operations status.
#
# Run order:
#   1. scripts/e2e_test.sh (creates "Client chat test 2" + Topic 1/2/3
#      via HTTP, while uvicorn is running)
#   2. stop uvicorn so the Telethon session file is free
#   3. this script (drives the CLI directly — it owns the Telethon client
#      for the duration of each subcommand)
#
# This script is non-destructive: members bulk-remove uses --dry-run, the
# new "CLI Topic" is closed but not deleted, and re-runs are idempotent.

set -euo pipefail

FOLDER="${FOLDER:-Clients}"
CHAT_TITLE="${CHAT_TITLE:-Client chat test 2}"
SINGLE_TOPIC_NAME="${SINGLE_TOPIC_NAME:-CLI Topic}"
USER_TO_REMOVE="${USER_TO_REMOVE:-@popstas}"

need() {
    command -v "$1" >/dev/null 2>&1 || {
        echo "missing required tool: $1" >&2
        exit 1
    }
}
need jq
need telegram-assistant

if ss -tlnp 2>/dev/null | grep -q ':8085 '; then
    echo "uvicorn is still bound to :8085 — stop it before running CLI tests" >&2
    echo "(Telethon session.session can be owned by only one process)" >&2
    exit 1
fi

step() {
    echo
    echo ">>> $*"
}

step "CLI: health"
telegram-assistant health

step "CLI: folders inspect --folder-name '${FOLDER}'"
telegram-assistant folders inspect --folder-name "${FOLDER}" | jq .

step "CLI: topics create --chat-name '${CHAT_TITLE}' --topic-name '${SINGLE_TOPIC_NAME}'"
cli_topic_json=$(telegram-assistant topics create \
    --chat-name "${CHAT_TITLE}" \
    --folder-name "${FOLDER}" \
    --topic-name "${SINGLE_TOPIC_NAME}")
echo "${cli_topic_json}" | jq .
chat_id=$(echo "${cli_topic_json}" | jq -r '.telegram_chat_id')
topic_id=$(echo "${cli_topic_json}" | jq -r '.telegram_topic_id')
op_id=$(echo "${cli_topic_json}" | jq -r '.operation_id')
if [[ -z "${chat_id}" || -z "${topic_id}" ]]; then
    echo "could not extract chat_id/topic_id from CLI topic create output" >&2
    exit 1
fi

step "CLI: topics create (idempotent re-call)"
telegram-assistant topics create \
    --chat-name "${CHAT_TITLE}" \
    --folder-name "${FOLDER}" \
    --topic-name "${SINGLE_TOPIC_NAME}" | jq '{telegram_topic_id, replayed}'

step "CLI: messages send (targeted) into '${SINGLE_TOPIC_NAME}'"
telegram-assistant messages send \
    --chat-name "${CHAT_TITLE}" \
    --folder-name "${FOLDER}" \
    --topic-name "${SINGLE_TOPIC_NAME}" \
    --text "cli targeted ping"

step "CLI: messages send (mass mode) to folder '${FOLDER}', topic 'Topic 1'"
telegram-assistant messages send \
    --folder-name "${FOLDER}" \
    --topic-name "Topic 1" \
    --text "cli mass ping" \
    --mass | jq '{mode, sent, skipped, items}'

step "CLI: topics close --topic-name '${SINGLE_TOPIC_NAME}'"
telegram-assistant topics close \
    --chat-name "${CHAT_TITLE}" \
    --folder-name "${FOLDER}" \
    --topic-name "${SINGLE_TOPIC_NAME}" --reason "e2e CLI close"

step "CLI: topics close (idempotent re-close)"
telegram-assistant topics close \
    --chat-name "${CHAT_TITLE}" \
    --folder-name "${FOLDER}" \
    --topic-name "${SINGLE_TOPIC_NAME}" --reason "e2e CLI re-close"

step "CLI: members bulk-remove --dry-run (no destructive side-effect)"
remove_tmp=$(mktemp --suffix=.csv)
trap 'rm -f "${remove_tmp}"' EXIT
printf 'user\n%s\n' "${USER_TO_REMOVE}" > "${remove_tmp}"
telegram-assistant members bulk-remove \
    --chat-id "${chat_id}" \
    --file "${remove_tmp}" --dry-run | jq '{operation_status, dry_run, items}'

step "CLI: operations status --operation-id ${op_id}"
telegram-assistant operations status --operation-id "${op_id}" | jq .

echo
echo "cli e2e flow completed — review the responses above and the chats on Telegram"
