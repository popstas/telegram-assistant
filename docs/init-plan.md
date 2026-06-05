> ТЗ на Telegram-часть проекта `telegram-assistant`. Planfix, `@planfix_bot` и объекты `Lead`/`Chat` описаны только как контекст: их настройка, поля и сценарии не входят в это задание.

## Цель

Разработать `telegram-assistant` - Telegram-клиент/HTTP-сервис/CLI-инструмент для автоматизации операций в Telegram-группах, которые используются в интеграции Planfix ↔ Telegram.

Сервис должен работать через MTProto от имени технического Telegram-пользователя, потому что Bot API не умеет создавать группы, добавлять пользователей до старта диалога и выполнять часть административных действий, которые нужны проекту.

## Границы задания

Входит:

- создание клиентских Telegram supergroup с включёнными топиками;
- помещение созданных чатов в Telegram chat folder, указанную в конфиге;
- создание invite-ссылок;
- добавление `@planfix_bot`, менеджеров, сотрудников и резервных технических аккаунтов;
- массовое добавление пользователей в существующие группы;
- массовое удаление пользователей из групп;
- массовое создание топиков;
- закрытие топиков;
- HTTP API для всех операций;
- CLI-интерфейс для всех HTTP endpoints;
- идемпотентность, очередь, throttling и обработка Telegram `FLOOD_WAIT`;
- логи, мониторинг, healthcheck.

Не входит:

- создание объектов/полей/сценариев в Планфиксе;
- реализация своего Planfix Chat API-коннектора;
- замена штатного `@planfix_bot`;
- логика бизнес-статусов услуг;
- юридическое решение по самоудалению сообщений;
- WhatsApp;
- разграничение доступа по топикам внутри одной группы. Telegram этого не поддерживает: участник группы видит все топики.

## Контекст Planfix

Planfix будет вызывать сервис из сценариев или кнопок и передавать ему готовые данные:

- `planfix_task_id` - числовой ID задачи Планфикса для идемпотентности, логов и автоматической отправки `/task`;
- название Telegram-группы;
- короткие названия топиков;
- список админов;
- список участников;
- ID задач общения, к которым позже будет привязываться `@planfix_bot`.

Сервис возвращает Planfix технические данные Telegram:

- `telegram_chat_id`;
- `telegram_chat_invite_link`;
- `telegram_topic_id`;
- статус операции;
- понятную ошибку, если операция не выполнена.

Привязка топика к задаче через `/task <id>` относится к контексту интеграции. В этом ТЗ нужно только обеспечить возможность отправить сообщение/команду в нужный чат и топик, если это потребуется.

## Архитектура

`telegram-assistant` состоит из трёх интерфейсов над одной доменной логикой:

- **HTTP API** - основной интерфейс для Planfix/автоматизаций.
- **CLI** - ручной и пакетный запуск тех же операций администратором.
- **Worker/queue** - выполнение Telegram-операций с throttling и обработкой `FLOOD_WAIT`.

Внутри:

- Python 3.12+;
- Telethon для MTProto;
- FastAPI или аналогичный HTTP-фреймворк;
- SQLite для состояния операций;
- Docker-образ;
- конфиг в `data/config.yml`.

## Конфигурация

Минимальный конфиг:

```yaml
telegram:
  api_id: 123456
  api_hash: "telegram_api_hash"
  # proxy_url: "socks5://user:pass@host:1080"  # опционально; socks5/socks4/http/https
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
    topics_layout: "list"   # "list" | "tabs" — применяется после groups create

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
```

Требования:

- конфиг хранится по пути `data/config.yml`;
- директория `data/` должна быть в `.gitignore`;
- секреты можно хранить в `data/config.yml`, потому что файл не попадает в git;
- SQLite database и Telethon session по умолчанию тоже лежат внутри `data/`;
- chat folder задаётся в конфиге через `folder_name`, опционально с контрольным `folder_id`;
- если folder с таким `folder_name`/`folder_id` отсутствует, сервис возвращает понятную ошибку;
- chat folder нельзя создавать автоматически;
- `reserve_admins` и `reserve_members` из конфига добавляются CLI-командами по умолчанию, если не указан флаг `--no-reserve`;
- `enable_topics` и `create_invite_link` по умолчанию берутся из `telegram.defaults`.

## Chat Folder

Сервис должен работать с Telegram chat folders.

Требования:

- все новые клиентские группы помещаются в folder, указанную в конфиге;
- массовые операции должны уметь фильтровать группы по folder;
- должна быть CLI-команда для проверки folder и списка чатов в ней;
- должна быть CLI-команда для переноса существующих групп в указанную folder;
- во всех CLI-командах, где указывается folder, нужно поддержать `--folder-name`;
- если `--folder-name` не указан, CLI использует `telegram.default_chat_folder.folder_name` из `data/config.yml`;
- во всех CLI-командах, где нужен `chat_id`, нужно поддержать `--chat-name` в связке с `--folder-name`: CLI ищет чат по точному названию внутри указанной folder и подставляет `chat_id`;
- если `--chat-name` найден в folder несколько раз, команда должна завершиться ошибкой и показать совпадения;
- если Telegram API не позволяет выполнить folder-операцию для конкретного peer, операция должна перейти в `needs_review`, а не молча считаться успешной.

Ожидаемое поведение при создании группы:

1. создать supergroup;
2. включить topics;
3. добавить участников/админов;
4. создать invite link;
5. поместить группу в configured chat folder;
6. сохранить результат операции.

## HTTP API и CLI

У каждого HTTP endpoint должен быть CLI-аналог. CLI использует ту же доменную логику, что и HTTP API, а не отдельную реализацию.

Формат CLI:

```bash
telegram-assistant <resource> <action> [options]
```

Для каждого endpoint ниже указана обязательная CLI-команда.

## CLI-only: авторизация Telethon

Интерактивная авторизация технического Telegram-аккаунта выполняется только через CLI, без HTTP endpoint.

CLI:

```bash
telegram-assistant auth
```

Требования:

- команда запускает интерактивный логин Telethon;
- читает `api_id`, `api_hash` и `session_path` из `data/config.yml`;
- запрашивает phone/code/password в терминале;
- сохраняет Telethon session по `telegram.session_path`;
- повторный запуск для уже авторизованной session должен показать текущий аккаунт и не требовать повторного кода без необходимости;
- после успешного логина `telegram-assistant health` и `GET /health` должны возвращать `telegram_session = authorized`.

## Endpoint: создать группу

```http
POST /telegram/groups
```

CLI:

```bash
telegram-assistant groups create \
  --planfix-task-id 901569 \
  --title "Клиент / проект" \
  --admin @manager \
  --member @editor \
  --folder-name "Planfix clients"
```

Запрос:

```json
{
  "planfix_task_id": 901569,
  "title": "Клиент / проект",
  "admins": ["@manager"],
  "members": ["@editor"],
  "reserve_admins": ["@reserve_account"],
  "reserve_members": ["@planfix_bot"],
  "folder_name": "Planfix clients",
  "create_invite_link": true,
  "enable_topics": true
}
```

Ответ:

```json
{
  "status": "created",
  "created": true,
  "telegram_chat_id": "-1001234567890",
  "telegram_chat_invite_link": "https://t.me/+...",
  "folder_name": "Planfix clients"
}
```

Требования:

- создать supergroup;
- включить topics;
- если `enable_topics` не передан, брать `telegram.defaults.enable_topics` из конфига;
- если `create_invite_link` не передан, брать `telegram.defaults.create_invite_link` из конфига;
- добавить `@planfix_bot`, если он пришёл в `members`/`reserve_members`;
- добавить основного менеджера, участников и резервных админов;
- выдать admin-права переданным админам;
- создать invite link;
- поместить группу в configured chat folder;
- если `planfix_task_id` указан и в группе есть `@planfix_bot`, отправить в группу служебное сообщение `/task {planfix_task_id}`, например `/task 123456`;
- повторный вызов с тем же `planfix_task_id` не создаёт дубль;
- повторный вызов с тем же `title` тоже должен быть идемпотентным и возвращать существующий чат.

CLI-логика reserve:

- по умолчанию CLI добавляет `reserve_admins` и `reserve_members` из `data/config.yml`;
- флаг `--no-reserve` отключает добавление всех `reserve*` значений из конфига;
- значения, явно переданные через `--reserve-admin`/`--reserve-member`, добавляются поверх конфига.

## Endpoint: создать один топик

```http
POST /telegram/topics
```

CLI:

```bash
telegram-assistant topics create \
  --planfix-task-id 987654 \
  --chat-name "Клиент / проект" \
  --folder-name "Planfix clients" \
  --topic-name "Научная статья 1"
```

Запрос:

```json
{
  "planfix_task_id": 987654,
  "telegram_chat_id": "-1001234567890",
  "topic_name": "Научная статья 1",
  "message": null
}
```

Ответ:

```json
{
  "status": "created",
  "created": true,
  "telegram_chat_id": "-1001234567890",
  "telegram_topic_id": 42,
  "topic_name": "Научная статья 1"
}
```

Требования:

- создать forum topic в указанной группе;
- сохранить маппинг `planfix_task_id` -> `telegram_chat_id` + `telegram_topic_id`, если `planfix_task_id` указан;
- повторный вызов с тем же `planfix_task_id` возвращает существующий topic;
- CLI должен поддерживать `--chat-id` или связку `--chat-name` + `--folder-name`;
- CLI должен поддерживать `--topic-name`;
- после создания топика отправить первое сообщение:
  - если указан `planfix_task_id`, отправить `/task {planfix_task_id}`;
  - если `planfix_task_id` не указан, но указан `message`, отправить `message`;
  - если не указаны ни `planfix_task_id`, ни `message`, первым сообщением продублировать название топика.

## Endpoint: массовое создание топиков

```http
POST /telegram/topics/bulk-create
```

CLI:

```bash
telegram-assistant topics bulk-create \
  --chat-name "Клиент / проект" \
  --folder-name "Planfix clients" \
  --file topics.csv
```

Формат CSV:

```csv
planfix_task_id,topic_name,message
987654,Научная статья 1,
,Публикация в СМИ,Первое сообщение в топике
,Документы,
```

Запрос:

```json
{
  "telegram_chat_id": "-1001234567890",
  "topics": [
    { "planfix_task_id": 987654, "topic_name": "Научная статья 1", "message": null },
    { "topic_name": "Публикация в СМИ", "message": "Первое сообщение в топике" }
  ],
  "continue_on_error": true
}
```

Ответ:

```json
{
  "status": "completed",
  "created": 2,
  "existed": 0,
  "failed": 0,
  "items": [
    {
      "planfix_task_id": 987654,
      "status": "created",
      "telegram_topic_id": 42
    }
  ]
}
```

Требования:

- поддержать JSON и CSV input в CLI;
- CSV должен поддерживать колонки `planfix_task_id`, `topic_name`, `message`;
- выполнять операции через очередь;
- уважать Telegram `FLOOD_WAIT`;
- не создавать дубли по `planfix_task_id`, если он указан;
- если `planfix_task_id` не указан, идемпотентность item строится по `telegram_chat_id + topic_name`;
- после создания каждого топика отправить первое сообщение:
  - если указан `planfix_task_id`, отправить `/task {planfix_task_id}`;
  - если `planfix_task_id` не указан, но заполнен `message`, отправить `message`;
  - если не указаны ни `planfix_task_id`, ни `message`, отправить `topic_name`;
- при `continue_on_error = true` продолжать пакет после ошибки отдельного элемента.

## Endpoint: закрыть топик

```http
POST /telegram/topics/{topic_id}/close
```

CLI:

```bash
telegram-assistant topics close \
  --chat-name "Клиент / проект" \
  --folder-name "Planfix clients" \
  --topic-name "Научная статья 1"
```

Запрос:

```json
{
  "telegram_chat_id": "-1001234567890",
  "reason": "source_task_closed"
}
```

Ответ:

```json
{
  "status": "closed",
  "telegram_chat_id": "-1001234567890",
  "telegram_topic_id": 42
}
```

Требования:

- закрыть топик;
- не удалять топик и историю;
- CLI должен поддерживать `--topic-id` или `--topic-name`;
- `--topic-name` ищется внутри выбранного чата;
- повторный вызов для закрытого топика возвращает `closed`.

## Endpoint: массовое добавление пользователей

```http
POST /telegram/groups/{chat_id}/members/bulk-add
```

CLI:

```bash
telegram-assistant members bulk-add \
  --chat-name "Клиент / проект" \
  --folder-name "Planfix clients" \
  --file users.csv
```

Формат CSV:

```csv
user,role
@manager,admin
@editor,member
123456789,member
```

Запрос:

```json
{
  "users": [
    { "user": "@manager", "role": "admin" },
    { "user": "@editor", "role": "member" },
    { "user": 123456789, "role": "member" }
  ],
  "continue_on_error": true
}
```

Ответ:

```json
{
  "status": "completed",
  "added": 3,
  "already_present": 0,
  "failed": 0,
  "items": [
    { "user": "@manager", "status": "added", "role": "admin" }
  ]
}
```

Требования:

- принимать username, phone/contact ID или Telegram user ID там, где это возможно через MTProto;
- CLI должен поддерживать `--chat-id` или связку `--chat-name` + `--folder-name`;
- уметь назначать admin-права для `role = admin`;
- не падать всем пакетом из-за одного невалидного пользователя при `continue_on_error = true`;
- логировать причину ошибки по каждому пользователю;
- учитывать privacy restrictions Telegram.

## Endpoint: массовое удаление пользователей

```http
POST /telegram/groups/{chat_id}/members/bulk-remove
```

CLI:

```bash
telegram-assistant members bulk-remove \
  --chat-name "Клиент / проект" \
  --folder-name "Planfix clients" \
  --file users.csv
```

Формат CSV:

```csv
user
@editor
123456789
```

Запрос:

```json
{
  "users": ["@editor", 123456789],
  "mode": "ban_unban",
  "continue_on_error": true
}
```

Ответ:

```json
{
  "status": "completed",
  "removed": 2,
  "not_present": 0,
  "failed": 0,
  "items": [
    { "user": "@editor", "status": "removed" }
  ]
}
```

Требования:

- удалить пользователя из группы;
- CLI должен поддерживать `--chat-id` или связку `--chat-name` + `--folder-name`;
- режим по умолчанию: `ban_unban`, чтобы убрать из группы, но не оставить в blacklist навсегда;
- учитывать, что Telegram показывает служебное сообщение об удалении участника;
- не удалять технические аккаунты и `@planfix_bot`, если явно не передан `--force`;
- поддержать dry-run в CLI.

CLI dry-run:

```bash
telegram-assistant members bulk-remove \
  --chat-name "Клиент / проект" \
  --folder-name "Planfix clients" \
  --file users.csv \
  --dry-run
```

## Endpoint: отправить сообщение или команду

```http
POST /telegram/messages
```

CLI:

```bash
telegram-assistant messages send \
  --folder-name "Planfix clients" \
  --chat-name "Клиент / проект" \
  --topic-name "Научная статья 1" \
  --text "/task 123457"
```

Запрос:

```json
{
  "telegram_chat_id": "-1001234567890",
  "telegram_topic_id": 42,
  "folder_name": null,
  "chat_name": null,
  "topic_name": null,
  "text": "/task 123457"
}
```

Массовый запрос по folder и topic name:

```json
{
  "folder_name": "Planfix clients",
  "topic_name": "Документы",
  "text": "Обновили документы"
}
```

Ответ:

```json
{
  "status": "sent",
  "telegram_message_id": 777,
  "items": []
}
```

Ответ для массового режима:

```json
{
  "status": "completed",
  "sent": 12,
  "skipped": 3,
  "failed": 0,
  "items": [
    {
      "telegram_chat_id": "-1001234567890",
      "chat_name": "Клиент / проект",
      "topic_name": "Документы",
      "status": "sent",
      "telegram_message_id": 777
    },
    {
      "telegram_chat_id": "-1001234567891",
      "chat_name": "Другой клиент",
      "topic_name": "Документы",
      "status": "skipped",
      "reason": "topic_not_found"
    }
  ]
}
```

Требования:

- отправлять сообщение в группу или конкретный topic;
- CLI должен поддерживать `--chat-id` или связку `--chat-name` + `--folder-name`;
- CLI должен поддерживать `--topic-id` или `--topic-name`;
- если указаны только `--folder-name` и `--topic-name`, отправить сообщение в каждый топик с таким названием в каждой группе этой folder;
- если указаны только `folder_name` и `topic_name` в HTTP-запросе, выполнить такое же массовое отправление по folder;
- если в какой-то группе topic с указанным названием не существует, пропустить эту группу и отметить item как `skipped`;
- использовать для служебных команд вроде `/task <id>`;
- логировать факт отправки без утечки секретов.

## Endpoint: операции с chat folder

### Проверить folder

```http
GET /telegram/folders/{folder_name}
```

CLI:

```bash
telegram-assistant folders inspect --folder-name "Planfix clients"
```

Ответ:

```json
{
  "folder_id": 2,
  "folder_name": "Planfix clients",
  "chats_count": 15,
  "chats": [
    { "telegram_chat_id": "-1001234567890", "title": "Клиент / проект" }
  ]
}
```

### Переместить чат в folder

```http
POST /telegram/folders/{folder_name}/chats
```

CLI:

```bash
telegram-assistant folders add-chat \
  --folder-name "Planfix clients" \
  --chat-name "Клиент / проект"
```

Запрос:

```json
{
  "telegram_chat_id": "-1001234567890",
  "chat_name": "Клиент / проект"
}
```

Ответ:

```json
{
  "status": "added",
  "folder_id": 2,
  "folder_name": "Planfix clients",
  "telegram_chat_id": "-1001234567890"
}
```

## Endpoint: healthcheck

```http
GET /health
```

CLI:

```bash
telegram-assistant health
```

Ответ:

```json
{
  "status": "ok",
  "telegram_session": "authorized",
  "database": "ok",
  "default_folder": "ok"
}
```

## Идемпотентность

Сервис обязан защищаться от дублей и повторов.

Ключи идемпотентности:

- создание группы: `planfix_task_id`, если указан, иначе `title`;
- создание топика: `planfix_task_id`, если указан, иначе `telegram_chat_id + topic_name`;
- bulk item: `operation_id + item planfix_task_id/topic_name/user`;
- закрытие топика: `telegram_chat_id + telegram_topic_id`;
- добавление пользователя: `telegram_chat_id + user`;
- удаление пользователя: `telegram_chat_id + user`.

Состояния операции:

- `pending`;
- `completed`;
- `failed`;
- `needs_review`.

Повторный запрос:

- при `completed` возвращает сохранённый результат;
- при `pending` ждёт ограниченное время или возвращает `503`;
- при `failed` возвращает сохранённую ошибку;
- при `needs_review` не делает автоматический retry.

## Очередь и Telegram limits

Все операции, которые меняют Telegram-состояние, должны идти через очередь:

- создание групп;
- создание топиков;
- массовое создание топиков;
- добавление пользователей;
- удаление пользователей;
- выдача admin-прав;
- перенос чатов в folder.

Требования:

- ограничить параллелизм Telegram-операций;
- обрабатывать `FLOOD_WAIT` как штатную паузу, а не как fatal error;
- сохранять прогресс bulk-операций по каждому item;
- уметь продолжить пакет после рестарта worker;
- иметь CLI-команду просмотра статуса операции.

CLI:

```bash
telegram-assistant operations status --operation-id op_123
telegram-assistant operations retry --operation-id op_123
```

## Ошибки

Сервис должен различать:

- пользователь не найден;
- пользователя нельзя добавить из-за privacy settings;
- пользователь уже в группе;
- пользователь не состоит в группе;
- нет прав администратора;
- чат не найден;
- topic не найден;
- folder не найден;
- Telethon session не авторизована;
- Telegram вернул `FLOOD_WAIT`;
- операция имеет неопределённый исход после таймаута.

Неопределённый исход переводится в `needs_review`, автоматический повтор не выполняется.

## Логи и мониторинг

Логи должны быть структурными:

- `operation_id`;
- HTTP request ID или CLI invocation ID;
- `planfix_task_id`;
- `chat_name`;
- `topic_name`;
- `telegram_chat_id`;
- `telegram_topic_id`;
- тип операции;
- item для bulk-операции;
- результат;
- ошибка;
- длительность.

Минимальные алерты:

- Telethon session не авторизована;
- несколько `FLOOD_WAIT` подряд;
- bulk-операция зависла;
- операция перешла в `needs_review`;
- default chat folder недоступна;
- процент ошибок выше порога.

## Безопасность

- Telethon session не хранится в репозитории.
- Telethon session создаётся интерактивной командой `telegram-assistant auth`.
- HTTP token не хранится в репозитории.
- CLI не печатает секреты.
- Invite-ссылки считаются чувствительными данными.
- Массовое удаление пользователей требует подтверждения в CLI, если не передан `--yes`.
- Удаление технических аккаунтов и `@planfix_bot` требует `--force`.
- Все destructive bulk-команды поддерживают `--dry-run`.

## Тестовый контур

Для проверки нужен тестовый Telegram-контур:

- технический Telegram-аккаунт;
- резервный технический аккаунт;
- тестовая chat folder;
- `@planfix_bot`;
- 2-3 тестовых сотрудника;
- тестовая группа.

Проверить:

- интерактивный логин через `telegram-assistant auth`;
- авторизацию Telethon session;
- создание группы;
- автоматическое помещение группы в configured chat folder;
- повторное создание группы с тем же `planfix_task_id` без дубля;
- повторное создание группы с тем же `title` без дубля;
- создание одного топика;
- создание топика с первым сообщением `/task {planfix_task_id}`;
- создание топика без `planfix_task_id`, где первым сообщением становится `message`;
- создание топика без `planfix_task_id` и `message`, где первым сообщением становится название топика;
- массовое создание топиков из CSV с колонками `planfix_task_id`, `topic_name`, `message`;
- массовое добавление пользователей;
- массовое удаление пользователей с `--dry-run` и без него;
- отправку команды `/task <id>` в конкретный topic по `--topic-name`;
- массовую отправку сообщения по `--folder-name` + `--topic-name` во все группы folder, где такой topic существует;
- закрытие топика;
- обработку невалидного username;
- поведение при недоступной folder: сервис возвращает ошибку и не создаёт folder автоматически;
- CLI-аналог каждого HTTP endpoint.

## Критерии приёмки MVP

- Проект называется `telegram-assistant`.
- Сервис поднимается из Docker и проходит `GET /health`.
- CLI-команда `telegram-assistant auth` выполняет интерактивный логин Telethon и сохраняет session в `data/`.
- CLI-команда `telegram-assistant health` показывает тот же статус.
- Конфиг читается из `data/config.yml`, директория `data/` находится в `.gitignore`.
- HTTP-порт по умолчанию `8085`.
- Создание группы работает через HTTP и CLI.
- Новая группа попадает в chat folder из конфига.
- Chat folder не создаётся автоматически, если её нет.
- Повторный вызов создания группы с тем же `planfix_task_id` не создаёт дубль.
- Повторный вызов создания группы с тем же `title` не создаёт дубль.
- Если при создании группы указан `planfix_task_id` и в группе есть `@planfix_bot`, сервис отправляет `/task {planfix_task_id}`.
- Создание одного топика работает через HTTP и CLI.
- CLI умеет находить чат по `--chat-name` внутри `--folder-name`.
- CLI умеет находить топик по `--topic-name`.
- Массовое создание топиков работает через HTTP и CLI.
- `topics.csv` поддерживает `planfix_task_id`, `topic_name`, `message`.
- Массовое добавление пользователей работает через HTTP и CLI.
- Массовое удаление пользователей работает через HTTP и CLI, включая `--dry-run`.
- Закрытие топика работает через HTTP и CLI.
- Отправка сообщения/команды в topic работает через HTTP и CLI, включая режим `folder_name + topic_name`.
- Все bulk-операции возвращают результат по каждому item.
- `FLOOD_WAIT` обрабатывается очередью без потери операции.
- Ошибки Telegram видны в ответах, логах и статусе операции.

## Открытые решения перед разработкой

- Какой набор admin-прав выдавать менеджерам и резервным аккаунтам.
- Где будет хоститься сервис и кто смотрит алерты.
