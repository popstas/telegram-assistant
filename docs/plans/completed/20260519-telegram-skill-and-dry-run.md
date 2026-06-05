# Telegram Planfix Assistant — агентский скилл и общий --dry-run

## Overview

Добавить в проект `telegram-assistant` настоящий Codex/Claude skill (`./skills/telegram-assistant/SKILL.md`), который учит агента превращать человеческие просьбы в безопасные вызовы уже существующего CLI: понять resource/action из фразы, найти чат/folder/topic, при необходимости подготовить временный CSV/JSON в `/tmp`, выполнить `--dry-run`, дождаться явного подтверждения и только потом сделать реальное изменение.

Параллельно нужно довести CLI до состояния, при котором скилл будет безопасным: добавить `--dry-run` во все команды, меняющие состояние Telegram или очереди операций. Без нормального dry-run агентский скилл опасен, поэтому это часть одной фичи.

## Context

- Целевой проект: `telegram-assistant`. CLI уже умеет работать с Telegram (`groups`, `topics`, `members`, `messages`, `folders`, `operations`, `auth`, `health`).
- Конфиг проекта: `data/config.yml` (отсюда агент может брать default folder и прочее).
- Скилл живёт в репозитории: `./skills/telegram-assistant/SKILL.md`.
- HTTP API, сценарии Planfix и Telegram-логика в рамках этой фичи не меняются — скилл только описывает агенту, как пользоваться готовым CLI.
- В итоговом `SKILL.md` нельзя использовать реальные имена, usernames, названия клиентов и invite links — только обезличенные примеры (`@employee_username`, `Клиент / проект`, `Planfix clients`).
- Адаптировано из `data/plan-skill.md`.

## Development Approach

- Testing approach: regular
- Сначала закрывается общий `--dry-run` для всех меняющих команд, затем пишется скилл, который на него опирается
- Complete each task fully before moving to the next
- Update this plan when scope changes during implementation

## Testing Strategy

- Unit tests required for every code-changing Task (особенно для нового `--dry-run` поведения)
- Для скилла — проверка на обезличенных примерах из раздела «Проверка» исходного ТЗ
- Run project tests after each Task before proceeding

## Progress Tracking

- Mark completed items with `[x]` immediately when done
- Update plan if implementation deviates from original scope

## Technical Details

### Resources & actions, которые должен покрывать скилл

| Resource | Action | Когда выбирать | Команда |
|---|---|---|---|
| `auth` | `login` | Авторизовать технический Telegram-аккаунт | `telegram-assistant auth` |
| `health` | `check` | Проверить, что сервис, база, session и default folder живые | `telegram-assistant health` |
| `groups` | `create` | Создать клиентскую supergroup с топиками, участниками, invite link и folder | `telegram-assistant groups create ...` |
| `topics` | `create` | Создать один topic в существующей группе | `telegram-assistant topics create ...` |
| `topics` | `bulk-create` | Создать несколько topic из CSV/JSON | `telegram-assistant topics bulk-create ...` |
| `topics` | `close` | Закрыть topic, не удаляя историю | `telegram-assistant topics close ...` |
| `members` | `bulk-add` | Добавить участников/админов в группу | `telegram-assistant members bulk-add ...` |
| `members` | `bulk-remove` | Удалить участников из группы | `telegram-assistant members bulk-remove ...` |
| `messages` | `send` | Отправить сообщение или служебную команду в чат/topic | `telegram-assistant messages send ...` |
| `folders` | `inspect` | Проверить Telegram folder и список чатов в ней | `telegram-assistant folders inspect ...` |
| `folders` | `add-chat` | Переместить существующий чат в folder | `telegram-assistant folders add-chat ...` |
| `operations` | `status` | Посмотреть статус операции | `telegram-assistant operations status ...` |
| `operations` | `retry` | Повторить операцию, если допустимо | `telegram-assistant operations retry ...` |

### Команды, которым нужен `--dry-run`

| Команда | Что должен проверять dry-run |
|---|---|
| `groups create` | title, folder, admins, members, reserve accounts, возможность создать группу и invite link |
| `topics create` | chat, folder, topic name, optional `planfix_task_id`, первое сообщение |
| `topics bulk-create` | chat, folder, CSV/JSON, список топиков, дубли по topic name / `planfix_task_id` |
| `topics close` | chat, folder, topic, текущее состояние topic |
| `members bulk-add` | chat, folder, CSV, users, roles, кто уже есть в чате, кого нельзя добавить |
| `members bulk-remove` | chat, folder, CSV, users, кто есть/кого нет в чате, защита технических аккаунтов |
| `messages send` | chat/folder/topic, текст, массовый режим, список получателей |
| `folders add-chat` | folder, chat, текущее нахождение чата в folder |
| `operations retry` | operation id, текущий статус, можно ли повторять без риска дубля |

`auth`, `health`, `folders inspect`, `operations status` не меняют состояние — `--dry-run` им не нужен.

### Ожидаемый формат результата dry-run

- команда возвращает `status = dry_run`;
- в ответе видно, какие объекты найдены;
- в ответе видно, какие действия были бы выполнены;
- ошибки валидации возвращаются так же, как при реальном запуске;
- Telegram-состояние не меняется;
- операции в очереди не создаются, либо создаются только как dry-run-запись без выполнения.

### Общий алгоритм агента, который скилл должен описывать

1. Прочитать просьбу или пересланное сообщение.
2. Определить resource/action.
3. Извлечь параметры: chat, topic, users, role, text, planfix task id, folder.
4. Если не хватает обязательных данных — задать короткий уточняющий вопрос.
5. Перед изменениями проверить `telegram-assistant health`, если ещё не проверялось в сессии.
6. Для bulk-команд подготовить временный CSV/JSON в `/tmp`.
7. Для меняющих команд сначала выполнить `--dry-run`, если он поддерживается.
8. Показать план человеку: что найдено, какая команда, какой результат dry-run.
9. Дождаться явного подтверждения.
10. Запустить реальную команду.
11. Коротко вернуть результат: выполнено / уже было / пропущено / ошибка Telegram / нужен ручной разбор.

## Implementation Steps

### Task 1: Аудит CLI-команд и текущего состояния `--dry-run`

- [x] пройтись по всем командам из таблицы Resources & actions и зафиксировать, где `--dry-run` уже есть, а где его нет
- [x] для каждой меняющей команды зафиксировать, какие шаги валидации она уже делает, и какие нужно перевести в безопасный режим
- [x] определить общую структуру вывода `status = dry_run` (формат и поля), чтобы все команды отвечали единообразно
- [x] зафиксировать список «технических аккаунтов» и `@planfix_bot`, которые нельзя удалять без `--force`
- [x] write tests describing the expected shape of `--dry-run` output (общий контракт)
- [x] run project tests - must pass before next task

### Task 2: Добавить `--dry-run` в команды создания и изменения групп/топиков

- [x] добавить `--dry-run` в `groups create` (проверка title, folder, admins, members, reserve accounts, возможность создать группу и invite link)
- [x] добавить `--dry-run` в `topics create` (chat, folder, topic name, optional `planfix_task_id`, первое сообщение)
- [x] добавить `--dry-run` в `topics bulk-create` (chat, folder, CSV/JSON, дубли по topic name / `planfix_task_id`)
- [x] добавить `--dry-run` в `topics close` (chat, folder, topic, текущее состояние topic)
- [x] убедиться, что dry-run не создаёт операций в очереди (или создаёт только dry-run-запись)
- [x] write tests for new dry-run flag in groups/topics commands
- [x] run project tests - must pass before next task

### Task 3: Добавить `--dry-run` в команды членства и сообщений

- [x] добавить `--dry-run` в `members bulk-add` (chat, folder, CSV, users, roles, кто уже есть, кого нельзя добавить)
- [x] добавить `--dry-run` в `members bulk-remove` (chat, folder, CSV, users, кто есть/кого нет, защита технических аккаунтов и `@planfix_bot` без `--force`)
- [x] добавить `--dry-run` в `messages send` (chat/folder/topic, текст, массовый режим по folder + topic name, список получателей)
- [x] убедиться, что в dry-run не отправляется ни одно сообщение и не меняется ни один состав группы
- [x] write tests for new dry-run flag in members/messages commands
- [x] run project tests - must pass before next task

### Task 4: Добавить `--dry-run` в команды folders и operations

- [x] добавить `--dry-run` в `folders add-chat` (folder, chat, текущее нахождение чата в folder)
- [x] добавить `--dry-run` в `operations retry` (operation id, текущий статус, можно ли повторять без риска дубля)
- [x] убедиться, что `auth`, `health`, `folders inspect`, `operations status` остались без `--dry-run` (они не меняют состояние)
- [x] синхронизировать сообщения об ошибках валидации между dry-run и реальным запуском, чтобы они были одинаковыми
- [x] write tests for folders/operations dry-run и для отсутствия флага у read-only команд
- [x] run project tests - must pass before next task

### Task 5: Создать каркас `SKILL.md` и общие правила работы агента

- [x] создать файл `./skills/telegram-assistant/SKILL.md` с YAML front-matter и заголовком
- [x] описать, где лежит конфиг (`data/config.yml`) и как им пользоваться (default folder и прочее)
- [x] описать команду `telegram-assistant health` как проверку живости перед любыми изменениями
- [x] зафиксировать правило: основной интерфейс агента — CLI, не прямые вызовы Telethon
- [x] описать общий алгоритм агента из 11 шагов (читать просьбу → resource/action → параметры → health → CSV → dry-run → план → подтверждение → запуск → результат)
- [x] прописать правила подготовки временных CSV/JSON в `/tmp`, не в репозитории
- [x] прописать правила подтверждения для destructive- и массовых команд (кнопка «Выполнить?» либо текстовое подтверждение)
- [x] write tests или проверки (например, lint/schema) для структуры SKILL.md, если в проекте есть подходящий механизм
- [x] run project tests - must pass before next task

### Task 6: Описать в `SKILL.md` все resource/action и сценарии

- [x] добавить таблицу Resources & actions со всеми 13 парами resource/action
- [x] для каждой пары описать: какие данные извлекать из просьбы, какие флаги обязательны, какие можно брать из конфига, нужен ли временный CSV/JSON, что автоматизировать, где нужно подтверждение, какие типовые ошибки показывать
- [x] добавить раздел сценария `groups create` с обезличенным примером команды (`--planfix-task-id`, `--title`, `--admin`, `--member`, `--folder-name`)
- [x] добавить сценарий `topics create` (поиск чата по `--chat-name` + `--folder-name`, `--topic-name`)
- [x] добавить сценарий `topics bulk-create` с примером временного CSV в `/tmp` и вызовом `--file`
- [x] добавить сценарий `topics close` с обязательным планом и подтверждением
- [x] добавить сценарий `members bulk-add` с временным CSV, обязательным `--dry-run`, затем повторным запуском без `--dry-run`
- [x] добавить сценарий `members bulk-remove` с особыми правилами: всегда `--dry-run`, защита технических аккаунтов и `@planfix_bot`, `--force` только по явному подтверждению
- [x] добавить сценарий `messages send` (одиночный и массовый по folder + topic name) с явным предупреждением о множественной рассылке
- [x] добавить сценарий `folders inspect` как read-only без подтверждения
- [x] добавить сценарий `folders add-chat` как изменение состояния с подтверждением
- [x] добавить сценарии `operations status` (без подтверждения) и `operations retry` (только после просмотра статуса и подтверждения)
- [x] описать `auth` как ручную команду для администратора — агент не запрашивает коды и пароли в чат
- [x] write tests или smoke-проверку, что в `SKILL.md` присутствуют все 13 resource/action
- [x] run project tests - must pass before next task

### Task 7: Описать ошибки, уточнения и границы скилла

- [x] прописать список ситуаций, когда агент обязан остановиться и спросить: непонятен resource/action; нет username/chat/topic/text/operation id; несколько похожих чатов или топиков; `health` показал проблему; dry-run вернул ошибку; затронут технический аккаунт; запрошено действие, которого нет в списке
- [x] добавить короткие шаблоны уточнений без канцелярита («не вижу username, кого добавить?», «нашёл несколько чатов с похожим названием, какой выбрать?», «dry-run упал: чат не найден. Проверь название?»)
- [x] прописать раздел «Что не входит»: не писать ещё одного Telegram-бота, не делать новый HTTP-сервис, не добавлять Planfix-сценарии, не ходить напрямую в Telethon, не менять Telegram без подтверждения, не угадывать чат/топик при неточном совпадении, не использовать реальные имена/usernames/инвайты в примерах
- [x] перечитать получившийся `SKILL.md` и убедиться, что во всех примерах используются обезличенные `@employee_username`, `@manager_username`, `@member_username`, «Клиент / проект», «Planfix clients»
- [x] write tests или линт-проверку обезличенности примеров, если это легко автоматизировать (опционально)
- [x] run project tests - must pass before next task

### Task 8: Verify acceptance criteria

- [x] прогнать скилл на обезличенном примере «добавь @employee_username в чат "Клиент / проект"» — сценарий задокументирован в SKILL.md (members bulk-add): CSV в /tmp, --dry-run, ожидание подтверждения, реальный запуск
- [x] прогнать скилл на «создай топики "Документы" и "Оплата" в чате "Клиент / проект"» — сценарий topics bulk-create в SKILL.md с CSV в /tmp и --dry-run
- [x] прогнать скилл на «отправь /task 123456 в топик "Документы" чата "Клиент / проект"» — сценарий messages send (targeted) в SKILL.md с --dry-run
- [x] убедиться, что агент корректно обрабатывает «уже существует», «чат не найден», «topic не найден», «username не найден», «несколько похожих совпадений», `FLOOD_WAIT`, `needs_review` — покрыто разделами "Typical errors" по парам и "Clarification templates"
- [x] проверить, что для всех меняющих команд `--dry-run` реализован и возвращает `status = dry_run` с найденными объектами и списком будущих действий — verified via grep across cli/main.py for all 9 changing commands
- [x] verify all requirements from Overview are implemented
- [x] run full project test suite — 404/404 passed
- [x] run project linter — ruff: All checks passed

## Post-Completion

*Items requiring manual intervention - no checkboxes, informational only*

- Команду `auth` агент не вызывает автоматически — администратор запускает её вручную при необходимости перелогина технического Telegram-аккаунта.
- Удаление технического аккаунта или `@planfix_bot` всегда требует отдельного человеческого решения и флага `--force`; в автоматизацию это не закладывается.
- Если в будущем появятся новые resource/action, их нужно добавить и в CLI, и в `SKILL.md` параллельно, иначе агент о них «не узнает».
