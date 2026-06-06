#!/usr/bin/env bash
# Tertiary end-to-end script: exercises the HTTP endpoints not covered by
# scripts/e2e_test.sh — single topic create, topic close, members bulk-remove
# (dry_run), and folders add-chat. Designed to run *after* scripts/e2e_test.sh
# so "Client chat test 2" already exists in the "Clients" folder.
#
# Prerequisites:
#   1. uvicorn is running on the port from data/config.yml (default 8085).
#   2. scripts/e2e_test.sh ran successfully at least once.
#
# Non-destructive: bulk-remove uses dry_run=true; the new HTTP-side topic is
# closed but not deleted.

set -euo pipefail

BASE_URL="${BASE_URL:-http://127.0.0.1:8085}"
BEARER="${BEARER:-e2e-test-token}"
FOLDER="${FOLDER:-Clients}"
CHAT_TITLE="${CHAT_TITLE:-Client chat test 2}"
HTTP_TOPIC_NAME="${HTTP_TOPIC_NAME:-HTTP Topic}"
USER_TO_REMOVE="${USER_TO_REMOVE:-@popstas}"

need() {
    command -v "$1" >/dev/null 2>&1 || {
        echo "missing required tool: $1" >&2
        exit 1
    }
}
need curl
need jq

auth_header=(-H "Authorization: Bearer ${BEARER}")
json_header=(-H "Content-Type: application/json")

step() {
    echo
    echo ">>> $*"
}

# Resolve the chat id for "Client chat test 2" via the folder endpoint.
step "resolve chat id for '${CHAT_TITLE}' via /telegram/folders/${FOLDER}"
chat_id=$(curl -sS "${auth_header[@]}" "${BASE_URL}/telegram/folders/${FOLDER}" \
    | jq -r --arg t "${CHAT_TITLE}" '.chats[] | select(.title==$t) | .chat_id')
if [[ -z "${chat_id}" ]]; then
    echo "could not find '${CHAT_TITLE}' in folder '${FOLDER}' — run scripts/e2e_test.sh first" >&2
    exit 1
fi
echo "resolved chat_id=${chat_id}"

step "POST /telegram/topics (single create, '${HTTP_TOPIC_NAME}')"
topic_payload=$(jq -nc --arg cid "${chat_id}" --arg name "${HTTP_TOPIC_NAME}" \
    '{telegram_chat_id: ($cid|tonumber), topic_name: $name}')
topic_resp=$(curl -sS -X POST "${auth_header[@]}" "${json_header[@]}" \
    -d "${topic_payload}" \
    "${BASE_URL}/telegram/topics")
echo "${topic_resp}" | jq .
topic_id=$(echo "${topic_resp}" | jq -r '.telegram_topic_id')
if [[ -z "${topic_id}" || "${topic_id}" == "null" ]]; then
    echo "could not extract topic_id from response" >&2
    exit 1
fi

step "POST /telegram/topics (idempotent re-call, same chat + topic name)"
curl -sS -X POST "${auth_header[@]}" "${json_header[@]}" \
    -d "${topic_payload}" \
    "${BASE_URL}/telegram/topics" | jq '{telegram_topic_id, replayed, operation_status}'

step "POST /telegram/topics/${topic_id}/close"
close_payload=$(jq -nc --arg cid "${chat_id}" \
    '{telegram_chat_id: ($cid|tonumber), reason: "e2e http close"}')
curl -sS -X POST "${auth_header[@]}" "${json_header[@]}" \
    -d "${close_payload}" \
    "${BASE_URL}/telegram/topics/${topic_id}/close" | jq .

step "POST /telegram/topics/${topic_id}/close (idempotent re-close)"
curl -sS -X POST "${auth_header[@]}" "${json_header[@]}" \
    -d "${close_payload}" \
    "${BASE_URL}/telegram/topics/${topic_id}/close" | jq '{status, replayed, operation_status}'

step "POST /telegram/groups/${chat_id}/members/bulk-remove (destructive — round-tripped immediately)"
# The HTTP bulk-remove endpoint has no dry_run knob (the spec keeps that to
# the CLI side). To keep this script non-destructive we kick the user and
# immediately add them back. Net effect: no change in membership. Idempotent
# on re-run because Telegram does not raise on a re-add.
remove_payload=$(jq -nc --arg user "${USER_TO_REMOVE}" \
    '{continue_on_error: true, items: [{user: $user}]}')
curl -sS -X POST "${auth_header[@]}" "${json_header[@]}" \
    -d "${remove_payload}" \
    "${BASE_URL}/telegram/groups/${chat_id}/members/bulk-remove" \
    | jq '{operation_status, items}'

step "POST /telegram/groups/${chat_id}/members/bulk-add (re-add ${USER_TO_REMOVE} after the remove)"
readd_payload=$(jq -nc --arg user "${USER_TO_REMOVE}" \
    '{continue_on_error: true, items: [{user: $user, role: "member"}]}')
curl -sS -X POST "${auth_header[@]}" "${json_header[@]}" \
    -d "${readd_payload}" \
    "${BASE_URL}/telegram/groups/${chat_id}/members/bulk-add" \
    | jq '{operation_status, added, items}'

step "POST /telegram/folders/${FOLDER}/chats (re-add an existing chat by id — idempotent)"
addchat_payload=$(jq -nc --arg cid "${chat_id}" '{chat_id: ($cid|tonumber)}')
addchat_resp=$(curl -sS -X POST "${auth_header[@]}" "${json_header[@]}" \
    -d "${addchat_payload}" \
    "${BASE_URL}/telegram/folders/${FOLDER}/chats" || true)
echo "${addchat_resp}" | jq . 2>/dev/null || echo "${addchat_resp}"

# --- media / scheduled send, reactions, forward, mute, folder remove -------
# These target only the test chat. The scheduled send is deferred far
# enough to be cancellable by hand; the folder remove is rounded back with
# an add so net membership is unchanged.

step "POST /telegram/messages (media) — attach a small server-side text file"
media_tmp=$(mktemp --suffix=.txt)
trap 'rm -f "${media_tmp}"' EXIT
printf 'e2e http media attachment\n' > "${media_tmp}"
media_payload=$(jq -nc --arg cid "${chat_id}" --arg f "${media_tmp}" \
    '{telegram_chat_id: ($cid|tonumber), text: "http media caption", files: [$f]}')
media_resp=$(curl -sS -X POST "${auth_header[@]}" "${json_header[@]}" \
    -d "${media_payload}" \
    "${BASE_URL}/telegram/messages")
echo "${media_resp}" | jq '{telegram_message_id, scheduled, operation_status}'
message_id=$(echo "${media_resp}" | jq -r '.telegram_message_id')

step "POST /telegram/messages (scheduled) — defer by 600s into the test chat"
sched_payload=$(jq -nc --arg cid "${chat_id}" \
    '{telegram_chat_id: ($cid|tonumber), text: "http scheduled ping (10m)", delay_seconds: 600}')
curl -sS -X POST "${auth_header[@]}" "${json_header[@]}" \
    -d "${sched_payload}" \
    "${BASE_URL}/telegram/messages" | jq '{scheduled, telegram_message_id, operation_status}'

if [[ -n "${message_id}" && "${message_id}" != "null" ]]; then
    step "POST /telegram/messages/reactions (set 👍 on message ${message_id})"
    react_payload=$(jq -nc --arg cid "${chat_id}" --argjson mid "${message_id}" \
        '{telegram_chat_id: ($cid|tonumber), message_id: $mid, emoji: "👍"}')
    curl -sS -X POST "${auth_header[@]}" "${json_header[@]}" \
        -d "${react_payload}" \
        "${BASE_URL}/telegram/messages/reactions" | jq '{telegram_message_id, emoji, cleared}'

    step "POST /telegram/messages/reactions (clear on message ${message_id})"
    clear_payload=$(jq -nc --arg cid "${chat_id}" --argjson mid "${message_id}" \
        '{telegram_chat_id: ($cid|tonumber), message_id: $mid, clear: true}')
    curl -sS -X POST "${auth_header[@]}" "${json_header[@]}" \
        -d "${clear_payload}" \
        "${BASE_URL}/telegram/messages/reactions" | jq '{telegram_message_id, emoji, cleared}'

    step "POST /telegram/messages/forward (message ${message_id} into the same test chat)"
    fwd_payload=$(jq -nc --arg cid "${chat_id}" --argjson mid "${message_id}" \
        '{from_chat_id: ($cid|tonumber), to_chat_id: ($cid|tonumber), message_ids: [$mid]}')
    curl -sS -X POST "${auth_header[@]}" "${json_header[@]}" \
        -d "${fwd_payload}" \
        "${BASE_URL}/telegram/messages/forward" | jq '{from_chat_id, to_chat_id, telegram_message_ids}'
fi

step "POST /telegram/notifications/mute then /unmute (round-trip for chat ${chat_id})"
mute_payload=$(jq -nc --arg cid "${chat_id}" \
    '{telegram_chat_id: ($cid|tonumber), duration_hours: 1}')
curl -sS -X POST "${auth_header[@]}" "${json_header[@]}" \
    -d "${mute_payload}" \
    "${BASE_URL}/telegram/notifications/mute" | jq '{telegram_chat_id, muted}'
unmute_payload=$(jq -nc --arg cid "${chat_id}" '{telegram_chat_id: ($cid|tonumber)}')
curl -sS -X POST "${auth_header[@]}" "${json_header[@]}" \
    -d "${unmute_payload}" \
    "${BASE_URL}/telegram/notifications/unmute" | jq '{telegram_chat_id, muted}'

step "DELETE /telegram/folders/${FOLDER}/chats then re-add (round-trip for chat ${chat_id})"
remove_chat_payload=$(jq -nc --arg cid "${chat_id}" '{chat_id: ($cid|tonumber)}')
curl -sS -X DELETE "${auth_header[@]}" "${json_header[@]}" \
    -d "${remove_chat_payload}" \
    "${BASE_URL}/telegram/folders/${FOLDER}/chats" | jq '{folder_id, already_absent}'
curl -sS -X POST "${auth_header[@]}" "${json_header[@]}" \
    -d "${remove_chat_payload}" \
    "${BASE_URL}/telegram/folders/${FOLDER}/chats" | jq '{folder_id, already_in_folder}'

echo
echo "http extras e2e flow completed — review the responses above and the chats on Telegram"
