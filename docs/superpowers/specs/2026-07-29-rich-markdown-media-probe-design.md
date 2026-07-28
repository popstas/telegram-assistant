# rich_markdown: единый путь заполнения media-атрибутов (ffprobe/ffmpeg)

**Дата:** 2026-07-29
**Закрывает:** два пункта `docs/TODO.md` — «анимированный `.gif` не прикрепляется» и «крупные видео уходят с заглушечными атрибутами и рендерятся пустыми».

## Проблема

`utils.get_attributes()` (telethon) без `hachoir` не извлекает метаданные медиа. Сейчас
`_document_attributes()` (`src/telegram_assistant/messages/telethon_backend.py:210`) затыкает
это точечно, по одному частному случаю на симптом:

- **audio** — без `hachoir` не выдаётся ни одного `DocumentAttributeAudio`, поэтому `.mp3`
  недостижим через `tg://audio`; добавляем `DocumentAttributeAudio(duration=0)`.
- **`.gif`** — mime `image/gif`, не `video/*`, поэтому `DocumentAttributeVideo` не выводится;
  добавляем `DocumentAttributeAnimated()`. **Проверено пользователем: этого недостаточно —
  анимированный gif в статью так не прикрепляется, файл нужно конвертировать в mp4** (Telegram
  и в обычных чатах хранит «гифки» как немой mp4-документ с `DocumentAttributeAnimated`).
- **video** — для **любого** mp4 возвращается заглушка
  `DocumentAttributeVideo(duration=0, w=1, h=1, supports_streaming=False, video_codec=None)`.
  Дальше метаданные чинит сервер, переразбирая загруженный файл, но не для всех файлов.

Замер live (2026-07-29, Избранное, msg 407429/407430, статья с 10 видео): у семи видео ≤ 6.30 МБ
сервер проставил реальные `duration` (1.7–109.6 с), реальные `w`/`h`, `thumbs=2`,
`supports_streaming=True`, `video_codec` h264/hevc. У трёх ≥ 12.72 МБ осталось
`duration=0, w=1, h=1`, `thumbs=None`, `video_codec=None` — клиент, получив 1×1 без длительности
и без превью, рисует пустой прямоугольник. Порог серверной обработки где-то между 6.30 и 12.72 МБ
и нигде не документирован.

Общий корень один: мы полагаемся на чужое (telethon без метадата-библиотеки, затем сервер) там,
где можем заполнить атрибуты сами. Дизайн заменяет три частных случая одним путём.

## Решения (подтверждены)

1. **Пробер — внешние `ffprobe`/`ffmpeg` через `subprocess`**, не pip-зависимость. `hachoir` для
   mp4 частичный и не даёт превью, а конвертацию gif не решает вовсе.
2. **Деградация:** бинаря нет или проб упал → видео уходит с нынешней заглушкой + warning в лог;
   `.gif` **отклоняется** до открытия operation-строки (без конвертации он гарантированно не
   прикрепится, тихо слать заведомо битое медиа нельзя).
3. **Объём:** пробуем **каждый** видео- и аудиофайл, без порога по размеру; для видео
   дополнительно генерируем превью. Порог Telegram недокументирован — зашивать его в код нельзя.
4. **Конвертация:** только `.gif` → mp4. Остальные видеоконтейнеры (`.mov`/`.webm`/`.mkv`/`.avi`)
   только пробуем — транскодирование десятков мегабайт на каждую отправку медленно и неожиданно.
5. **Warnings** про заглушку — только в лог. Контракт бэкенда не меняется, legacy-фейки не
   трогаем. Заодно закрывается жалоба из TODO: сейчас заливка 34 файлов не пишет **ни одной**
   строки в лог даже при `logging.level: DEBUG`.

## Архитектура

### Новый модуль `src/telegram_assistant/messages/media_probe.py`

Чистый, без импорта telethon — тривиально тестируется подменой `subprocess.run`.

```python
@dataclass(frozen=True)
class MediaProbe:
    duration: float        # секунды, 0.0 если не определилась
    width: int | None
    height: int | None
    has_video: bool
    has_audio: bool

class MediaConversionError(ValueError): ...

def ffprobe_available() -> bool: ...      # shutil.which, кэш на процесс
def ffmpeg_available() -> bool: ...
def probe_media(path: Path | str) -> MediaProbe | None: ...
def extract_thumbnail(path: Path | str, *, duration: float) -> bytes | None: ...
def convert_gif_to_mp4(path: Path | str) -> Path: ...
```

- `probe_media` — `ffprobe -v error -print_format json -show_format -show_streams <path>`,
  `subprocess.run` **без** `shell`, таймаут ~20 с. **Любая** неудача (нет бинаря, ненулевой код,
  кривой JSON, таймаут, отсутствие видеопотока) → `None`, никогда не исключение: у вызывающего
  есть корректный фолбэк, а отправка не должна падать из-за пробера.
- `extract_thumbnail` — `ffmpeg -ss <10% длительности> -i <path> -frames:v 1 -vf scale=320:-2
  -f mjpeg -`, таймаут ~30 с, вывод в stdout (без временного файла). Неудача → `None`.
- `convert_gif_to_mp4` — `ffmpeg -i <gif> -movflags +faststart -pix_fmt yuv420p -an -y <tmp.mp4>`,
  таймаут ~120 с. Файл создаётся через `tempfile.mkstemp(suffix=".mp4")`; при неудаче временный
  файл удаляется и поднимается `MediaConversionError` с хвостом stderr. `MediaConversionError` —
  подкласс `ValueError`, поэтому попадает на существующий путь 400 / exit 2, а не в пустой 500.

`ffprobe_available`/`ffmpeg_available` кэшируются (`functools.lru_cache`), но кэш очищается в
тестах через `cache_clear`.

### `_document_attributes()` — единственная точка заполнения

Сигнатура не меняется: `_document_attributes(path: str, kind: str) -> tuple[list[Any], str]`.
Внутри `utils.get_attributes()` остаётся только источником `DocumentAttributeFilename` и mime;
видео/аудио-атрибут строится из `probe_media()`:

- `kind == "video"`, проб удался и `has_video` → заменить/добавить
  `DocumentAttributeVideo(duration=round(probe.duration), w=probe.width, h=probe.height,
  supports_streaming=True)`.
- `kind == "audio"` → `DocumentAttributeAudio(duration=round(probe.duration))` вместо нынешнего
  жёсткого `0`.
- Проб не удался → нынешнее поведение (заглушка от telethon / `duration=0`) плюс
  `logger.warning` с именем файла, его размером и причиной (нет ffprobe / проб упал).

Атрибут, выведенный telethon, **заменяется**, а не дополняется: два `DocumentAttributeVideo` в
одном документе — некорректный запрос.

### Превью для видео

`extract_thumbnail()` → `client.upload_file(thumb_bytes, file_name="thumb.jpg")` → передаётся
как `thumb=` в `InputMediaUploadedDocument`. Именно отсутствие превью даёт визуально пустой
прямоугольник, поэтому превью генерируется для всех видео, а не только для крупных. Неудача
извлечения или загрузки превью — не ошибка отправки: `thumb` просто не передаётся, пишется
warning.

### `.gif` → mp4

В `_upload_rich_files` (`telethon_backend.py:410`), перед `upload_file`: если суффикс `.gif`,
файл конвертируется во временный mp4 и **грузится он**. Атрибуты конвертированного файла:
проб mp4 + `DocumentAttributeAnimated()` + `DocumentAttributeFilename(<исходный stem>.mp4)`,
mime `video/mp4`.

Markdown **не переписывается**: ссылка и так `tg://video?id=…`, `media_kind()` и `scan_media`
не трогаем — конвертация целиком про форму аплоада.

Временные файлы (конвертированный mp4) удаляются в `finally` вокруг цикла аплоада — включая путь
исключения, где `_translate_rich_send_error` уже поднимает наружу.

### Гейт `.gif` без ffmpeg

В `_validate_rich_files` (`src/telegram_assistant/messages/service.py:291`): если среди
`rich_files` есть файл с суффиксом `.gif`, а `ffmpeg_available()` ложно — `ValueError` с именем
файла и текстом «установите ffmpeg или сконвертируйте файл в mp4». Место выбрано потому, что эта
функция уже обращается к ФС и выполняется **до** открытия operation-строки: ключ идемпотентности
остаётся свободным для повторной попытки после установки ffmpeg.

### Логирование

`logging.getLogger(__name__)` в `telethon_backend.py` (сейчас в модуле логгера нет вообще):

- `logger.info` на каждый загружаемый файл: имя, kind, размер, определившаяся длительность/размеры;
- `logger.warning` на каждый фолбэк (нет пробера, проб упал, превью не получилось).

## Обработка ошибок

| Ситуация | Поведение |
|---|---|
| `ffprobe` отсутствует | видео/аудио уходят с заглушкой + warning |
| `ffprobe` упал/таймаут на файле | то же, warning называет файл |
| `.gif` + нет `ffmpeg` | `ValueError` до operation-строки, exit 2 / 400 |
| `.gif` + конвертация упала | `MediaConversionError` (`ValueError`) → exit 2 / 400, хвост stderr в сообщении |
| превью не извлеклось/не загрузилось | `thumb` не передаётся, warning, отправка продолжается |

Ни одна из этих ситуаций не меняет уже существующую трансляцию ошибок отправки
(`RichMediaForbidden`, `MessageSendUnconfirmed`, `FloodWaitError`).

## Тесты

- `media_probe`: `probe_media` парсит реальный JSON ffprobe (фикстура-строка), возвращает `None`
  на ненулевом коде / кривом JSON / таймауте / отсутствии бинаря; `convert_gif_to_mp4` чистит
  временный файл и поднимает `MediaConversionError` при неудаче; `extract_thumbnail` возвращает
  `None` при неудаче.
- `_document_attributes` с подменённым пробером: видео получает реальные `duration`/`w`/`h` и
  `supports_streaming=True`; аудио получает реальную длительность; при `probe_media() is None`
  сохраняется нынешняя заглушка и для `.gif` остаётся `DocumentAttributeAnimated`.
- `_upload_rich_files`: `.gif` грузится как конвертированный mp4 с `DocumentAttributeAnimated`,
  временный файл удаляется; видео получает `thumb=`; отсутствие превью не ломает отправку.
- `_validate_rich_files`: `.gif` без ffmpeg → `ValueError` с именем файла, operation-строка не
  открывается; с ffmpeg — проходит.

Живой e2e не требуется и **не запускается** (аккаунт уже блокировали за подозрительную
активность). Ручная проверка — по явному запросу пользователя, одна отправка в Избранное.

## Документация

- **CLAUDE.md** — рядом с остальными проверенными media-фактами: единый путь заполнения атрибутов,
  `ffprobe`/`ffmpeg` как опциональная внешняя зависимость, поведение при её отсутствии, `.gif`
  конвертируется в mp4, порог серверной починки (6.30–12.72 МБ) и почему на него нельзя опираться.
- **README** — `ffmpeg` в требованиях как опциональный (что ломается без него).
- **skills/telegram-assistant/SKILL.md** + ресинк в `~/.claude/skills/telegram-assistant/SKILL.md`.
- **docs/TODO.md** — оба пункта отмечены выполненными.

## Вне объёма

- Пункт TODO про `part=True` и post-send верификацию медиа — отдельная задача.
- Конвертация видеоконтейнеров кроме `.gif`.
- `hachoir` как зависимость.
