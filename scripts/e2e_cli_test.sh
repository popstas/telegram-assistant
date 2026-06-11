#!/usr/bin/env bash
# Secondary end-to-end script: exercises CLI surfaces against the same
# resources that scripts/e2e_test.sh creates over HTTP. Keeps coverage
# for the CLI subcommands defined in the plan:
#   * health, folders inspect, topics create + close, messages send,
#     members bulk-remove (--dry-run), operations status.
#   * newer message ergonomics: messages recent --minutes, messages send
#     --reply-to, and messages delete (session-limit ON blocks an unrecorded
#     id under the default delete_only_session_messages: true; a temp config
#     opting out + --no-revoke lets a delete through). The delete steps are
#     self-cleaning — each run sends fresh throwaway messages.
#   * access allowlist now uses the independent-capability model: a folder rule
#     must grant permissions: [read, write] for a read to be allowed (write no
#     longer implies read).
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

# Skip cleanly when no authorized Telethon session is present (e.g. a CI box
# without real credentials). The session path is read from data/config.yml; if
# the file is absent the live e2e is recorded as skipped rather than failing.
: "${SOURCE_CONFIG:=data/config.yml}"
session_path=""
if [[ -f "${SOURCE_CONFIG}" ]]; then
    session_path=$(awk -F': *' '/^[[:space:]]*session_path:/{print $2; exit}' \
        "${SOURCE_CONFIG}" 2>/dev/null | tr -d "\"'" | tr -d '[:space:]')
fi
if [[ -z "${session_path}" || ! -f "${session_path}" ]]; then
    echo "SKIP: no authorized Telethon session (session_path='${session_path:-unset}' not found) — live CLI e2e skipped" >&2
    exit 0
fi

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

# --- topic rename round-trip (idempotent: rename then rename back) ----------
# Rename "CLI Topic" -> "CLI Topic Renamed" by name, assert the new title, then
# rename back to the original so the close steps below still address it by name.
TOPIC_RENAMED="${SINGLE_TOPIC_NAME} Renamed"

step "CLI: topics rename --topic-name '${SINGLE_TOPIC_NAME}' -> '${TOPIC_RENAMED}'"
telegram-assistant topics rename \
    --chat-name "${CHAT_TITLE}" \
    --folder-name "${FOLDER}" \
    --topic-name "${SINGLE_TOPIC_NAME}" \
    --new-title "${TOPIC_RENAMED}" \
    --reason "e2e CLI topic rename" | jq '{telegram_topic_id, old_title, new_title, status, replayed}'

step "CLI: topics rename (idempotent re-call, same new title replays)"
telegram-assistant topics rename \
    --chat-name "${CHAT_TITLE}" \
    --folder-name "${FOLDER}" \
    --topic-name "${TOPIC_RENAMED}" \
    --new-title "${TOPIC_RENAMED}" | jq '{telegram_topic_id, new_title, replayed}'

step "CLI: topics rename back '${TOPIC_RENAMED}' -> '${SINGLE_TOPIC_NAME}'"
telegram-assistant topics rename \
    --chat-name "${CHAT_TITLE}" \
    --folder-name "${FOLDER}" \
    --topic-name "${TOPIC_RENAMED}" \
    --new-title "${SINGLE_TOPIC_NAME}" \
    --reason "e2e CLI topic rename back" | jq '{telegram_topic_id, new_title, status}'

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

# --- get-recent read op + entity resolver ----------------------------------

step "CLI: messages recent --chat-name '${CHAT_TITLE}' (default limit 5)"
recent_json=$(telegram-assistant messages recent \
    --chat-name "${CHAT_TITLE}" \
    --folder-name "${FOLDER}")
echo "${recent_json}" | jq '{telegram_chat_id, limit, count}'
recent_count=$(echo "${recent_json}" | jq -r '.count')
if [[ "${recent_count}" -gt 5 ]]; then
    echo "messages recent returned ${recent_count} > 5 with the default limit" >&2
    exit 1
fi

step "CLI: messages recent --entity '${CHAT_TITLE}' (exact-title resolver, --limit 3)"
telegram-assistant messages recent \
    --entity "${CHAT_TITLE}" \
    --limit 3 | jq '{telegram_chat_id, limit, count}'

step "CLI: messages recent --entity '${chat_id}' (numeric resolver)"
telegram-assistant messages recent --entity "${chat_id}" --limit 1 \
    | jq '{telegram_chat_id, count}'

step "CLI: messages recent --chat-id ${chat_id} --minutes 60 (windowed read)"
telegram-assistant messages recent \
    --chat-id "${chat_id}" --minutes 60 --limit 3 \
    | jq '{telegram_chat_id, minutes, count}'

# --- reply_to send ---------------------------------------------------------

step "CLI: messages send (reply_to) — send a parent then reply to it"
parent_json=$(telegram-assistant messages send \
    --chat-id "${chat_id}" \
    --text "cli parent for reply")
echo "${parent_json}" | jq '{telegram_message_id}'
parent_id=$(echo "${parent_json}" | jq -r '.telegram_message_id')
if [[ -n "${parent_id}" && "${parent_id}" != "null" ]]; then
    telegram-assistant messages send \
        --chat-id "${chat_id}" \
        --text "cli reply" \
        --reply-to "${parent_id}" | jq '{telegram_message_id, scheduled}'
fi

# --- delete message (session-limited on/off) -------------------------------
# Each CLI invocation is its own process with an empty SentMessageRegistry, so
# with the default delete_only_session_messages: true an arbitrary id is
# rejected (demonstrates the "on" guard). Opting out via a temp config
# (delete_only_session_messages: false) plus --no-revoke (delete only this
# account's own copy) lets a delete go through. Self-cleaning: a fresh throwaway
# message is sent each run and removed.

step "CLI: messages delete (session-limit ON) — fresh process must be blocked"
blocked_json=$(telegram-assistant messages send \
    --chat-id "${chat_id}" \
    --text "cli delete target (should be blocked under session limit)")
blocked_id=$(echo "${blocked_json}" | jq -r '.telegram_message_id')
if telegram-assistant messages delete \
        --chat-id "${chat_id}" \
        --message-id "${blocked_id}" --no-revoke >/dev/null 2>&1; then
    echo "expected session-limit to block delete of an unrecorded id, but it succeeded" >&2
    exit 1
fi
echo "ok: delete blocked under delete_only_session_messages: true (id ${blocked_id})"

step "CLI: messages delete (session-limit OFF) — temp config opts out, --no-revoke"
del_config=$(mktemp --suffix=.yml)
trap 'rm -f "${remove_tmp:-}" "${acl_config:-}" "${del_config:-}"' EXIT
: "${SOURCE_CONFIG:=data/config.yml}"
python3 - "${del_config}" "${SOURCE_CONFIG}" <<'PY'
import sys
from pathlib import Path

import yaml

out, src = sys.argv[1], sys.argv[2]
data = yaml.safe_load(Path(src).read_text(encoding="utf-8"))
access = data["telegram"].setdefault("access", {})
access["delete_only_session_messages"] = False
Path(out).write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
print(f"wrote delete-opt-out config to {out}")
PY
telegram-assistant messages delete \
    --chat-id "${chat_id}" \
    --message-id "${blocked_id}" \
    --no-revoke \
    --config "${del_config}" | jq '{deleted, message_ids, dry_run}'

# --- media / scheduled send, reactions, forward, folder round-trip ---------
# All of these target only the test chat created above; they are
# conservative and re-runnable. The scheduled send lands in the test chat
# only and is deferred far enough to be cancellable by hand if needed.

step "CLI: messages send (media) — attach a small generated text file"
media_tmp=$(mktemp --suffix=.txt)
printf 'e2e media attachment\n' > "${media_tmp}"
media_json=$(telegram-assistant messages send \
    --chat-id "${chat_id}" \
    --text "cli media caption" \
    --file "${media_tmp}")
echo "${media_json}" | jq '{telegram_message_id, scheduled}'
rm -f "${media_tmp}"
react_message_id=$(echo "${media_json}" | jq -r '.telegram_message_id')

step "CLI: messages send (scheduled) — defer by 10m into the test chat"
telegram-assistant messages send \
    --chat-id "${chat_id}" \
    --text "cli scheduled ping (10m)" \
    --delay 10m | jq '{scheduled, telegram_message_id}'

step "CLI: messages react --emoji 👍 on message ${react_message_id} (dry-run)"
telegram-assistant messages react \
    --chat-id "${chat_id}" \
    --message-id "${react_message_id}" \
    --emoji 👍 --dry-run | jq .

if [[ -n "${react_message_id}" && "${react_message_id}" != "null" ]]; then
    step "CLI: messages react --emoji 👍 on message ${react_message_id} (set)"
    telegram-assistant messages react \
        --chat-id "${chat_id}" \
        --message-id "${react_message_id}" \
        --emoji 👍 | jq '{telegram_message_id, emoji, cleared}'

    step "CLI: messages react --clear on message ${react_message_id} (remove)"
    telegram-assistant messages react \
        --chat-id "${chat_id}" \
        --message-id "${react_message_id}" \
        --clear | jq '{telegram_message_id, emoji, cleared}'

    step "CLI: messages forward — forward message ${react_message_id} into the same test chat"
    telegram-assistant messages forward \
        --from-chat-id "${chat_id}" \
        --to-chat-id "${chat_id}" \
        --message-id "${react_message_id}" | jq '{from_chat_id, to_chat_id, telegram_message_ids}'
fi

step "CLI: notifications mute --chat-id ${chat_id} (dry-run, 1h window)"
telegram-assistant notifications mute \
    --chat-id "${chat_id}" --duration 1 --dry-run | jq .

step "CLI: folders remove-chat then add-chat (round-trip for chat ${chat_id})"
telegram-assistant folders remove-chat \
    --chat-id "${chat_id}" \
    --folder-name "${FOLDER}" | jq '{folder_id, already_absent}'
telegram-assistant folders add-chat \
    --chat-id "${chat_id}" \
    --folder-name "${FOLDER}" | jq '{folder_id, already_in_folder}'

# --- group rename round-trip (idempotent: rename then rename back) ----------
# Rename "Client chat test 2" -> "<title> (renamed)" by id, assert via groups
# read-back is implicit in the result payload, then rename back so every later
# --chat-name "${CHAT_TITLE}" lookup (and other scripts) still resolve.
CHAT_RENAMED="${CHAT_TITLE} (renamed)"

step "CLI: groups rename --chat-id ${chat_id} -> '${CHAT_RENAMED}'"
telegram-assistant groups rename \
    --chat-id "${chat_id}" \
    --new-title "${CHAT_RENAMED}" \
    --reason "e2e CLI group rename" | jq '{telegram_chat_id, old_title, new_title, status, replayed}'

step "CLI: groups rename (idempotent re-call, same new title replays)"
telegram-assistant groups rename \
    --chat-id "${chat_id}" \
    --new-title "${CHAT_RENAMED}" | jq '{telegram_chat_id, new_title, replayed}'

step "CLI: groups rename --dry-run (must not mutate) back to '${CHAT_TITLE}'"
telegram-assistant groups rename \
    --chat-id "${chat_id}" \
    --new-title "${CHAT_TITLE}" --dry-run | jq '{dry_run, status, would}'

step "CLI: groups rename back '${CHAT_RENAMED}' -> '${CHAT_TITLE}'"
telegram-assistant groups rename \
    --chat-id "${chat_id}" \
    --new-title "${CHAT_TITLE}" \
    --reason "e2e CLI group rename back" | jq '{telegram_chat_id, new_title, status}'

# --- access allowlist (deny-by-default) ------------------------------------
# Derive a config that allows WRITE only on folder "${FOLDER}"; everything
# else is denied. A permitted read succeeds; an unlisted chat (Saved Messages
# via @me) returns a loud access-denied non-zero exit.

step "CLI: access allowlist — derive a folder-scoped policy"
acl_config=$(mktemp --suffix=.yml)
trap 'rm -f "${remove_tmp:-}" "${acl_config:-}" "${del_config:-}"' EXIT
: "${SOURCE_CONFIG:=data/config.yml}"
# Permissions are now INDEPENDENT (write no longer implies read), so the
# folder rule must list both read and write for the permitted-read step below
# to be granted. Uses the multi-permission list form added in this batch.
python3 - "${FOLDER}" "${acl_config}" "${SOURCE_CONFIG}" <<'PY'
import sys
from pathlib import Path

import yaml

folder, out, src = sys.argv[1], sys.argv[2], sys.argv[3]
data = yaml.safe_load(Path(src).read_text(encoding="utf-8"))
data["telegram"]["access"] = {
    "rules": [{"folder": folder, "permissions": ["read", "write"]}]
}
Path(out).write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
print(f"wrote allowlist config to {out}")
PY

step "CLI: access allowlist — permitted read (chat in '${FOLDER}') succeeds"
telegram-assistant messages recent \
    --chat-name "${CHAT_TITLE}" \
    --folder-name "${FOLDER}" \
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
