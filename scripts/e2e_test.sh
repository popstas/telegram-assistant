#!/usr/bin/env bash
# End-to-end test against a running telegram-assistant server.
#
# Prerequisites (manual, see Task 17 in docs/plans/20260518-telegram-assistant-mvp.md):
#   1. data/config.yml exists with valid api_id / api_hash / bearer_token.
#   2. data/sessions/expertizemeAssistant/session.session is an authorized
#      Telethon session for the test account (use `telegram-assistant auth`
#      to create one if missing).
#   3. The test account is a member of a Telegram folder called "Clients" that
#      contains a chat named "Client chat test".
#   4. The server is running locally on the port from data/config.yml
#      (default 8085), e.g.:
#        uvicorn telegram_assistant.http_api:create_app --factory --port 8085
#
# What this script verifies:
#   - GET  /health
#   - GET  /telegram/folders/Clients
#   - POST /telegram/groups            (creates "Client chat test 2")
#   - POST /telegram/topics/bulk-create (Topic 1/2/3 in the new group)
#   - POST /telegram/groups/{chat_id}/members/bulk-add (adds @popstas)
#   - POST /telegram/messages          (mass send to folder + topic)
#   - POST /telegram/messages          (reply_to: parent + threaded reply)
#   - GET  /telegram/messages/recent   (?minutes= windowed read)
#   - POST /telegram/messages/delete   (delete a message this process just
#                                       sent — session-limited, self-cleaning)
#
# Re-runnable: every mutating call is idempotent, so re-running is safe. The
# delete step relies on the default delete_only_session_messages: true and only
# removes a throwaway message this same server process sent moments earlier.

set -euo pipefail

BASE_URL="${BASE_URL:-http://127.0.0.1:8085}"
BEARER="${BEARER:-e2e-test-token}"
FOLDER="${FOLDER:-Clients}"
NEW_CHAT_TITLE="${NEW_CHAT_TITLE:-Client chat test 2}"
PLANFIX_TASK_ID="${PLANFIX_TASK_ID:-e2e-task-1}"
USER_TO_ADD="${USER_TO_ADD:-@popstas}"

need() {
    command -v "$1" >/dev/null 2>&1 || {
        echo "missing required tool: $1" >&2
        exit 1
    }
}
need curl
need jq

# Skip cleanly when no authorized Telethon session is present (the live e2e
# requires the authorized session the uvicorn server owns; without it the
# mutating endpoints return 503). The session path is read from data/config.yml.
: "${SOURCE_CONFIG:=data/config.yml}"
session_path=""
if [[ -f "${SOURCE_CONFIG}" ]]; then
    session_path=$(awk -F': *' '/^[[:space:]]*session_path:/{print $2; exit}' \
        "${SOURCE_CONFIG}" 2>/dev/null | tr -d "\"'" | tr -d '[:space:]')
fi
if [[ -z "${session_path}" || ! -f "${session_path}" ]]; then
    echo "SKIP: no authorized Telethon session (session_path='${session_path:-unset}' not found) — live e2e skipped" >&2
    exit 0
fi

auth_header=(-H "Authorization: Bearer ${BEARER}")
json_header=(-H "Content-Type: application/json")

step() {
    echo
    echo ">>> $*"
}

step "health"
curl -sS "${BASE_URL}/health" | jq .

step "inspect folder '${FOLDER}'"
folder_response=$(curl -sS "${auth_header[@]}" "${BASE_URL}/telegram/folders/${FOLDER}")
echo "${folder_response}" | jq .

step "create group '${NEW_CHAT_TITLE}'"
create_payload=$(jq -nc \
    --arg title "${NEW_CHAT_TITLE}" \
    --arg pid "${PLANFIX_TASK_ID}" \
    --arg folder "${FOLDER}" \
    '{title: $title, planfix_task_id: $pid, folder_name: $folder, members: [], admins: []}')
group_response=$(curl -sS -X POST "${auth_header[@]}" "${json_header[@]}" \
    -d "${create_payload}" \
    "${BASE_URL}/telegram/groups")
echo "${group_response}" | jq .
chat_id=$(echo "${group_response}" | jq -r '.telegram_chat_id // empty')

if [[ -z "${chat_id}" ]]; then
    echo "could not extract telegram_chat_id from response — aborting" >&2
    exit 1
fi

step "bulk-create topics in chat ${chat_id}"
bulk_topics_payload=$(jq -nc \
    --arg cid "${chat_id}" \
    '{telegram_chat_id: ($cid|tonumber), continue_on_error: true, items: [
        {topic_name: "Topic 1"},
        {topic_name: "Topic 2"},
        {topic_name: "Topic 3"}
    ]}')
curl -sS -X POST "${auth_header[@]}" "${json_header[@]}" \
    -d "${bulk_topics_payload}" \
    "${BASE_URL}/telegram/topics/bulk-create" | jq .

step "bulk-add ${USER_TO_ADD} to chat ${chat_id}"
add_payload=$(jq -nc --arg user "${USER_TO_ADD}" \
    '{continue_on_error: true, items: [{user: $user, role: "member"}]}')
curl -sS -X POST "${auth_header[@]}" "${json_header[@]}" \
    -d "${add_payload}" \
    "${BASE_URL}/telegram/groups/${chat_id}/members/bulk-add" | jq .

step "mass send to folder '${FOLDER}', topic 'Topic 1'"
mass_payload=$(jq -nc --arg folder "${FOLDER}" \
    '{folder_name: $folder, topic_name: "Topic 1", text: "e2e ping"}')
curl -sS -X POST "${auth_header[@]}" "${json_header[@]}" \
    -d "${mass_payload}" \
    "${BASE_URL}/telegram/messages" | jq .

step "reply_to: send a parent into chat ${chat_id}, then reply to it"
parent_payload=$(jq -nc --arg cid "${chat_id}" \
    '{telegram_chat_id: ($cid|tonumber), text: "e2e parent for reply"}')
parent_resp=$(curl -sS -X POST "${auth_header[@]}" "${json_header[@]}" \
    -d "${parent_payload}" \
    "${BASE_URL}/telegram/messages")
echo "${parent_resp}" | jq '{telegram_message_id}'
parent_id=$(echo "${parent_resp}" | jq -r '.telegram_message_id // empty')
if [[ -n "${parent_id}" ]]; then
    reply_payload=$(jq -nc --arg cid "${chat_id}" --argjson pid "${parent_id}" \
        '{telegram_chat_id: ($cid|tonumber), text: "e2e reply", reply_to_message_id: $pid}')
    curl -sS -X POST "${auth_header[@]}" "${json_header[@]}" \
        -d "${reply_payload}" \
        "${BASE_URL}/telegram/messages" | jq '{telegram_message_id, scheduled}'
fi

step "messages recent ?minutes=60 for chat ${chat_id} (limit 3)"
curl -sS "${auth_header[@]}" \
    "${BASE_URL}/telegram/messages/recent?chat_id=${chat_id}&limit=3&minutes=60" \
    | jq '{telegram_chat_id, minutes, limit, count}'

step "delete: send a throwaway message into chat ${chat_id}, then delete it"
del_payload=$(jq -nc --arg cid "${chat_id}" \
    '{telegram_chat_id: ($cid|tonumber), text: "e2e delete target (self-cleaning)"}')
del_resp=$(curl -sS -X POST "${auth_header[@]}" "${json_header[@]}" \
    -d "${del_payload}" \
    "${BASE_URL}/telegram/messages")
echo "${del_resp}" | jq '{telegram_message_id}'
del_id=$(echo "${del_resp}" | jq -r '.telegram_message_id // empty')
if [[ -n "${del_id}" ]]; then
    delete_payload=$(jq -nc --arg cid "${chat_id}" --argjson mid "${del_id}" \
        '{telegram_chat_id: ($cid|tonumber), message_ids: [$mid], revoke: true}')
    curl -sS -X POST "${auth_header[@]}" "${json_header[@]}" \
        -d "${delete_payload}" \
        "${BASE_URL}/telegram/messages/delete" | jq '{deleted, message_ids, dry_run}'
fi

echo
echo "e2e flow completed — review the responses above and the chats on Telegram"
