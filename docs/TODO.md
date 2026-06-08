# TODO

- [ ] apply config.yml changes without restart, for example, telegram.access rules
- [ ] add access rules commands to cli
- [ ] add reply_to option to messages send: cli, http, mcp, skill
- [ ] add delete message command, default behavior: delete message for all
- [ ] add access permission `delete`
- [ ] add access global option to limit delete message only to messages sent by telegram-assistant in current session. mcp server should store all messages sent by telegram-assistant in current session. In memory, not in database.
- [ ] access rules: allow to define multiple chats and multiple permissions for the same rule, for example, `write` and `delete` for the same chats.
- [ ] messages recent: add option to limit by minutes: last 5 minutes
- [ ] mcp telegram_messages_send: reduce args: remove chat_name, folder_name, folder_id, files
- [ ] mcp: option to disable some tools at server side with prefix support: telegram_groups_*, telegram_topics_*, telegram_members_*, telegram_folders_*, telegram_notifications_*. 
- [ ] change skill: don't check health when no issues. AskUser for message to send.
- [ ] allow to send files with MCP `telegram_messages_send`: MCP supports binary resources/base64 blobs, but `tools/call` has no universal file-upload primitive; current implementation explicitly rejects server-local `files` paths and only accepts `file_urls`. Add a safe upload/attachment path, for example base64 attachment input with filename/mime/size limits, or client-provided MCP resource URI ingestion when supported by clients.
- [ ] make MCP/HTTP `file_urls` reliable for Telegram upload: when Telethon passes a remote URL directly, Telegram may fail with `Failure while fetching the webpage with cURL` even if the URL is reachable from this server. Download http(s) URLs to temporary files server-side with size/time limits, send those local temp files via Telethon, and clean them up after send.
- [ ] reduce logs from `/health`: remove Telethon logs, keep only HTTP server logs.
