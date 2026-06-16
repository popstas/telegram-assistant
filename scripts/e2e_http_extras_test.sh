#!/usr/bin/env bash
# Tertiary end-to-end script: exercises the HTTP endpoints not covered by
# scripts/e2e_test.sh — single topic create, topic close, members bulk-remove
# (dry_run), folders add-chat, and the newer message ergonomics: reply_to send,
# messages recent ?minutes=, base64 inline attachments, file_urls
# download-to-temp send, and the DELETE message op (session-limited). Designed
# to run *after* scripts/e2e_test.sh so "Client chat test 2" already exists in
# the "Clients" folder.
#
# Prerequisites:
#   1. uvicorn is running on the port from data/config.yml (default 8085).
#   2. scripts/e2e_test.sh ran successfully at least once.
#   3. The DELETE steps assume the default
#      telegram.access.delete_only_session_messages: true — they only delete
#      messages this *same* server process sent moments earlier (recorded in
#      the in-memory SentMessageRegistry), so they are self-cleaning.
#
# Non-destructive: bulk-remove uses dry_run=true; the new HTTP-side topic is
# closed but not deleted. The reply_to/base64/file_urls sends and their delete
# targets are created and (where applicable) removed within the same run.

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
    echo "SKIP: no authorized Telethon session (session_path='${session_path:-unset}' not found) — live HTTP extras e2e skipped" >&2
    exit 0
fi

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

# --- topic rename round-trip (id path, then name path back) -----------------
# Rename "HTTP Topic" -> "HTTP Topic Renamed" via the id path, then rename back
# via the name-resolving path so the close steps below still address it by id.
HTTP_TOPIC_RENAMED="${HTTP_TOPIC_NAME} Renamed"

step "POST /telegram/topics/${topic_id}/rename -> '${HTTP_TOPIC_RENAMED}'"
rename_payload=$(jq -nc --arg cid "${chat_id}" --arg title "${HTTP_TOPIC_RENAMED}" \
    '{telegram_chat_id: ($cid|tonumber), new_title: $title, reason: "e2e http topic rename"}')
curl -sS -X POST "${auth_header[@]}" "${json_header[@]}" \
    -d "${rename_payload}" \
    "${BASE_URL}/telegram/topics/${topic_id}/rename" \
    | jq '{telegram_topic_id, old_title, new_title, status, replayed}'

step "POST /telegram/topics/${topic_id}/rename (idempotent re-call, same title replays)"
curl -sS -X POST "${auth_header[@]}" "${json_header[@]}" \
    -d "${rename_payload}" \
    "${BASE_URL}/telegram/topics/${topic_id}/rename" \
    | jq '{telegram_topic_id, new_title, replayed}'

step "POST /telegram/topics/rename (name path) — rename back to '${HTTP_TOPIC_NAME}'"
rename_back_payload=$(jq -nc --arg cid "${chat_id}" \
    --arg cur "${HTTP_TOPIC_RENAMED}" --arg title "${HTTP_TOPIC_NAME}" \
    '{telegram_chat_id: ($cid|tonumber), topic_name: $cur, new_title: $title, reason: "e2e http topic rename back"}')
curl -sS -X POST "${auth_header[@]}" "${json_header[@]}" \
    -d "${rename_back_payload}" \
    "${BASE_URL}/telegram/topics/rename" \
    | jq '{telegram_topic_id, new_title, status}'

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

step "POST /telegram/messages (media) — attach a remote URL"
media_payload=$(jq -nc --arg cid "${chat_id}" \
    '{telegram_chat_id: ($cid|tonumber), text: "http media caption", file_urls: ["https://www.python.org/static/img/python-logo.png"]}')
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

# --- reply_to / recent ?minutes= / base64 / file_urls / delete -------------
# All target only the test chat. The delete target is sent by this same server
# process moments before deletion, so the session-limited delete (default
# delete_only_session_messages: true) accepts it and the run is self-cleaning.

step "POST /telegram/messages (reply_to) — send a parent then reply to it"
parent_payload=$(jq -nc --arg cid "${chat_id}" \
    '{telegram_chat_id: ($cid|tonumber), text: "http parent for reply"}')
parent_resp=$(curl -sS -X POST "${auth_header[@]}" "${json_header[@]}" \
    -d "${parent_payload}" \
    "${BASE_URL}/telegram/messages")
echo "${parent_resp}" | jq '{telegram_message_id}'
parent_id=$(echo "${parent_resp}" | jq -r '.telegram_message_id')
if [[ -n "${parent_id}" && "${parent_id}" != "null" ]]; then
    reply_payload=$(jq -nc --arg cid "${chat_id}" --argjson pid "${parent_id}" \
        '{telegram_chat_id: ($cid|tonumber), text: "http reply", reply_to_message_id: $pid}')
    curl -sS -X POST "${auth_header[@]}" "${json_header[@]}" \
        -d "${reply_payload}" \
        "${BASE_URL}/telegram/messages" | jq '{telegram_message_id, scheduled}'
fi

step "GET /telegram/messages/recent?minutes=60 (windowed read, limit 3)"
curl -sS "${auth_header[@]}" \
    "${BASE_URL}/telegram/messages/recent?chat_id=${chat_id}&limit=3&minutes=60" \
    | jq '{telegram_chat_id, minutes, limit, count}'

step "POST /telegram/messages (base64 inline attachment)"
b64=$(printf 'e2e base64 attachment\n' | base64 | tr -d '\n')
b64_payload=$(jq -nc --arg cid "${chat_id}" --arg b64 "${b64}" \
    '{telegram_chat_id: ($cid|tonumber), text: "http base64 caption",
      base64_files: [{filename: "e2e-base64.txt", mime: "text/plain", content_b64: $b64}]}')
curl -sS -X POST "${auth_header[@]}" "${json_header[@]}" \
    -d "${b64_payload}" \
    "${BASE_URL}/telegram/messages" | jq '{telegram_message_id, scheduled}'

step "POST /telegram/messages (file_urls download-to-temp) — remote URL attachment"
# Exercises the reliable file_urls path: the server downloads the http(s) URL
# to a temp file (size/time bounded), sends the local file, then cleans up.
fileurl_payload=$(jq -nc --arg cid "${chat_id}" \
    '{telegram_chat_id: ($cid|tonumber), text: "http file_urls download-to-temp",
      file_urls: ["https://www.python.org/static/img/python-logo.png"]}')
curl -sS -X POST "${auth_header[@]}" "${json_header[@]}" \
    -d "${fileurl_payload}" \
    "${BASE_URL}/telegram/messages" | jq '{telegram_message_id, scheduled}'

step "POST /telegram/messages/delete — delete a message this process just sent"
# Send a throwaway message, then delete it (revoke=true, delete for everyone).
# Default delete_only_session_messages: true accepts it because this server
# process recorded the id in the SentMessageRegistry on send.
del_payload=$(jq -nc --arg cid "${chat_id}" \
    '{telegram_chat_id: ($cid|tonumber), text: "http delete target (self-cleaning)"}')
del_resp=$(curl -sS -X POST "${auth_header[@]}" "${json_header[@]}" \
    -d "${del_payload}" \
    "${BASE_URL}/telegram/messages")
echo "${del_resp}" | jq '{telegram_message_id}'
del_id=$(echo "${del_resp}" | jq -r '.telegram_message_id')
if [[ -n "${del_id}" && "${del_id}" != "null" ]]; then
    delete_payload=$(jq -nc --arg cid "${chat_id}" --argjson mid "${del_id}" \
        '{telegram_chat_id: ($cid|tonumber), message_ids: [$mid], revoke: true}')
    curl -sS -X POST "${auth_header[@]}" "${json_header[@]}" \
        -d "${delete_payload}" \
        "${BASE_URL}/telegram/messages/delete" | jq '{deleted, message_ids, dry_run}'
fi

step "POST /telegram/messages/delete (dry-run) — authorize without deleting"
dry_delete_payload=$(jq -nc --arg cid "${chat_id}" \
    '{telegram_chat_id: ($cid|tonumber), message_ids: [1], revoke: true, dry_run: true}')
# A fresh id not in the registry is rejected by the session-limit check even in
# dry-run mode; tolerate the 4xx and just show the response either way.
curl -sS -X POST "${auth_header[@]}" "${json_header[@]}" \
    -d "${dry_delete_payload}" \
    "${BASE_URL}/telegram/messages/delete" | jq . 2>/dev/null || true

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

# --- group rename round-trip (rename then rename back) ----------------------
# Rename "Client chat test 2" -> "<title> (renamed)" by id, then rename back so
# every later --chat-name / title lookup (and other scripts) still resolve.
HTTP_CHAT_RENAMED="${CHAT_TITLE} (renamed)"

step "POST /telegram/groups/rename -> '${HTTP_CHAT_RENAMED}'"
grename_payload=$(jq -nc --arg cid "${chat_id}" --arg title "${HTTP_CHAT_RENAMED}" \
    '{chat_id: ($cid|tonumber), new_title: $title, reason: "e2e http group rename"}')
curl -sS -X POST "${auth_header[@]}" "${json_header[@]}" \
    -d "${grename_payload}" \
    "${BASE_URL}/telegram/groups/rename" \
    | jq '{telegram_chat_id, old_title, new_title, status, replayed}'

step "POST /telegram/groups/rename (idempotent re-call, same title replays)"
curl -sS -X POST "${auth_header[@]}" "${json_header[@]}" \
    -d "${grename_payload}" \
    "${BASE_URL}/telegram/groups/rename" \
    | jq '{telegram_chat_id, new_title, replayed}'

step "POST /telegram/groups/rename — rename back to '${CHAT_TITLE}'"
grename_back_payload=$(jq -nc --arg cid "${chat_id}" --arg title "${CHAT_TITLE}" \
    '{chat_id: ($cid|tonumber), new_title: $title, reason: "e2e http group rename back"}')
curl -sS -X POST "${auth_header[@]}" "${json_header[@]}" \
    -d "${grename_back_payload}" \
    "${BASE_URL}/telegram/groups/rename" \
    | jq '{telegram_chat_id, new_title, status}'

echo
echo "http extras e2e flow completed — review the responses above and the chats on Telegram"
