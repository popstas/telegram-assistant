#!/usr/bin/env bash
# Secondary end-to-end script: exercises CLI surfaces against the same
# resources that scripts/e2e_test.sh creates over HTTP. Keeps coverage
# for the CLI subcommands defined in the plan:
#   * health, folders inspect, topics create + close, messages send,
#     media/scheduled messages send, messages react, messages forward,
#     notifications mute/unmute, members bulk-remove (--dry-run),
#     operations status.
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

cleanup_files=()
cleanup() {
    if [[ ${#cleanup_files[@]} -gt 0 ]]; then
        rm -f "${cleanup_files[@]}"
    fi
}
trap cleanup EXIT

step "CLI: health"
telegram-assistant health

step "CLI: folders inspect --folder-name '${FOLDER}'"
folder_json=$(telegram-assistant folders inspect --folder-name "${FOLDER}")
echo "${folder_json}" | jq .
chat_id=$(echo "${folder_json}" \
    | jq -r --arg t "${CHAT_TITLE}" '[.chats[] | select(.title==$t) | .chat_id] | last // empty')
if [[ -z "${chat_id}" || "${chat_id}" == "null" ]]; then
    echo "could not find '${CHAT_TITLE}' in folder '${FOLDER}' — run scripts/e2e_test.sh first" >&2
    exit 1
fi
echo "resolved chat_id=${chat_id}"

step "CLI: topics create --chat-id '${chat_id}' --topic-name '${SINGLE_TOPIC_NAME}'"
cli_topic_json=$(telegram-assistant topics create \
    --chat-id "${chat_id}" \
    --topic-name "${SINGLE_TOPIC_NAME}")
echo "${cli_topic_json}" | jq .
topic_id=$(echo "${cli_topic_json}" | jq -r '.telegram_topic_id')
op_id=$(echo "${cli_topic_json}" | jq -r '.operation_id')
if [[ -z "${chat_id}" || -z "${topic_id}" ]]; then
    echo "could not extract chat_id/topic_id from CLI topic create output" >&2
    exit 1
fi

step "CLI: topics create (idempotent re-call)"
telegram-assistant topics create \
    --chat-id "${chat_id}" \
    --topic-name "${SINGLE_TOPIC_NAME}" | jq '{telegram_topic_id, replayed}'

step "CLI: messages send (targeted) into '${SINGLE_TOPIC_NAME}'"
targeted_send_json=$(telegram-assistant messages send \
    --chat-id "${chat_id}" \
    --topic-name "${SINGLE_TOPIC_NAME}" \
    --text "cli targeted ping")
echo "${targeted_send_json}" | jq .
targeted_message_id=$(echo "${targeted_send_json}" \
    | jq -r '.telegram_message_id // (.telegram_message_ids[0] // empty)')
if [[ -z "${targeted_message_id}" || "${targeted_message_id}" == "null" ]]; then
    echo "could not extract telegram_message_id from targeted send output" >&2
    exit 1
fi

step "CLI: messages send --file (small temporary attachment)"
media_tmp=$(mktemp --suffix=.txt)
cleanup_files+=("${media_tmp}")
printf 'telegram-assistant CLI e2e attachment\n' > "${media_tmp}"
telegram-assistant messages send \
    --chat-id "${chat_id}" \
    --text "cli media ping" \
    --file "${media_tmp}" | jq '{telegram_message_id, telegram_message_ids, attachments}'

step "CLI: messages send --delay 10m (scheduled in test chat)"
telegram-assistant messages send \
    --chat-id "${chat_id}" \
    --text "cli scheduled ping" \
    --delay 10m | jq '{telegram_message_id, scheduled, schedule_at}'

step "CLI: messages react set + clear on targeted message ${targeted_message_id}"
telegram-assistant messages react \
    --chat-id "${chat_id}" \
    --message-id "${targeted_message_id}" \
    --emoji "👍" | jq .
telegram-assistant messages react \
    --chat-id "${chat_id}" \
    --message-id "${targeted_message_id}" \
    --clear | jq .

step "CLI: messages forward within test chat"
telegram-assistant messages forward \
    --from-chat-id "${chat_id}" \
    --to-chat-id "${chat_id}" \
    --message-id "${targeted_message_id}" \
    | jq '{source_chat_id, target_chat_id, message_ids, forwarded_message_ids}'

step "CLI: notifications mute/unmute round-trip"
telegram-assistant notifications mute \
    --chat-id "${chat_id}" \
    --duration 1 | jq .
telegram-assistant notifications unmute \
    --chat-id "${chat_id}" | jq .

step "CLI: messages send (mass mode) to folder '${FOLDER}', topic 'Topic 1'"
telegram-assistant messages send \
    --folder-name "${FOLDER}" \
    --topic-name "Topic 1" \
    --text "cli mass ping" \
    --mass | jq '{mode, sent, skipped, items}'

step "CLI: topics close --topic-name '${SINGLE_TOPIC_NAME}'"
telegram-assistant topics close \
    --chat-id "${chat_id}" \
    --topic-name "${SINGLE_TOPIC_NAME}" --reason "e2e CLI close"

step "CLI: topics close (idempotent re-close)"
telegram-assistant topics close \
    --chat-id "${chat_id}" \
    --topic-name "${SINGLE_TOPIC_NAME}" --reason "e2e CLI re-close"

step "CLI: members bulk-remove --dry-run (no destructive side-effect)"
remove_tmp=$(mktemp --suffix=.csv)
cleanup_files+=("${remove_tmp}")
printf 'user\n%s\n' "${USER_TO_REMOVE}" > "${remove_tmp}"
telegram-assistant members bulk-remove \
    --chat-id "${chat_id}" \
    --file "${remove_tmp}" --dry-run | jq '{operation_status, dry_run, items}'

step "CLI: operations status --operation-id ${op_id}"
telegram-assistant operations status --operation-id "${op_id}" | jq .

# --- get-recent read op + entity resolver ----------------------------------

step "CLI: messages recent --chat-id '${chat_id}' (default limit 5)"
recent_json=$(telegram-assistant messages recent \
    --chat-id "${chat_id}")
echo "${recent_json}" | jq '{telegram_chat_id, limit, count}'
recent_count=$(echo "${recent_json}" | jq -r '.count')
if [[ "${recent_count}" -gt 5 ]]; then
    echo "messages recent returned ${recent_count} > 5 with the default limit" >&2
    exit 1
fi

step "CLI: messages recent --entity '${chat_id}' (numeric resolver, --limit 3)"
telegram-assistant messages recent \
    --entity "${chat_id}" \
    --limit 3 | jq '{telegram_chat_id, limit, count}'

step "CLI: messages recent --entity '${chat_id}' (numeric resolver)"
telegram-assistant messages recent --entity "${chat_id}" --limit 1 \
    | jq '{telegram_chat_id, count}'

# --- access allowlist (deny-by-default) ------------------------------------
# Derive a config that allows WRITE only on folder "${FOLDER}"; everything
# else is denied. A permitted read succeeds; an unlisted chat (Saved Messages
# via @me) returns a loud access-denied non-zero exit.

step "CLI: access allowlist — derive a folder-scoped policy"
acl_config=$(mktemp --suffix=.yml)
cleanup_files+=("${acl_config}")
: "${SOURCE_CONFIG:=data/config.yml}"
python3 - "${FOLDER}" "${acl_config}" "${SOURCE_CONFIG}" <<'PY'
import sys
from pathlib import Path

import yaml

folder, out, src = sys.argv[1], sys.argv[2], sys.argv[3]
data = yaml.safe_load(Path(src).read_text(encoding="utf-8"))
data["telegram"]["access"] = {
    "rules": [{"folder": folder, "permission": "write"}]
}
Path(out).write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
print(f"wrote allowlist config to {out}")
PY

step "CLI: access allowlist — permitted read (chat in '${FOLDER}') succeeds"
telegram-assistant messages recent \
    --chat-id "${chat_id}" \
    --config "${acl_config}" | jq '{telegram_chat_id, count}'

step "CLI: access allowlist — unlisted chat (@me) must be denied (non-zero exit)"
if telegram-assistant messages recent \
        --entity "@me" \
        --config "${acl_config}" >/dev/null 2>&1; then
    echo "expected access-denied for an unlisted chat, but the call succeeded" >&2
    exit 1
fi
echo "ok: unlisted chat was denied as expected"

echo
echo "cli e2e flow completed — review the responses above and the chats on Telegram"
