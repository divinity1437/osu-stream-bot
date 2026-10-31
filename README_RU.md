# osu! replay stream

Локальный сервис для бесконечного osu!standard replay-стрима. Он принимает `.osr`, ставит их в очередь, рендерит видео через o!rdr или локальный danser и отдаёт готовый поток в браузерном источнике OBS.

## Возможности

- Web-player по адресу `/`, рассчитанный на OBS Browser Source.
- Панель управления по адресу `/control`.
- Загрузка osu! replay через браузер или копирование файлов в `inbox/`.
- Очередь видео с приоритетом Discord → локальные replay → случайные standard-score.
- Атомарное добавление готовых MP4 и предварительная проверка через `ffprobe`.
- Буферизация следующего ролика во время воспроизведения текущего.
- Переключение, пропуск и удаление видео из `/control`.
- Управление громкостью Browser Source с сохранением в `state.json`.
- Временная замена waiting-фона прямо из `/control`.
  - По умолчанию используется `static/fallback.svg`.
  - Можно загрузить изображение любого поддержанного браузером формата (`PNG`, `JPEG`, `GIF`, `WebP`, `SVG`, `AVIF` и другие image MIME types).
  - Загруженная картинка хранится только в RAM текущего процесса.
  - После перезапуска она удаляется автоматически, и снова используется `fallback.svg`.
  - Есть кнопка немедленного возврата к `fallback.svg`.
- Два backend рендера: hosted o!rdr и локальный danser-go.
- Чтение модификаторов из заголовка `.osr` и выбор DT/NC skin для o!rdr.
- Discord slash-команды для приёма replay без privileged Message Content intent.
- Защита от повторов, лимиты очереди и cooldown пользователей Discord.
- Англоязычное TUI `configure.sh` для настройки `.env` с подсказками, проверкой значений, скрытием секретов и резервной копией.
- Ротация логов в `logs/stream.log`.

## ROAD MAP

### Готово

- [x] Web-player для OBS с автоматическим переходом между видео.
- [x] Контрольная панель `/control`.
- [x] Загрузка `.osr` и ручная очередь через `inbox/`.
- [x] Удаление и пропуск текущего/ожидающего видео.
- [x] Буфер готовых роликов.
- [x] Hosted o!rdr renderer.
- [x] Локальный danser renderer с профилем 1280×720 @ 60 FPS.
- [x] Получение beatmap по MD5 и безопасная распаковка `.osz`.
- [x] Проверка результата через `ffprobe`.
- [x] Discord slash-команды `/submit`, `/status`, `/queue`, `/cancel`.
- [x] Временная загрузка фона с автоматическим сбросом при рестарте.
- [x] Настройка громкости из панели.
- [x] Восстановление завершённых o!rdr render через `--recover-render`.
- [x] Интерактивное TUI `configure.sh` для безопасной настройки `.env`.
- [x] MIT-лицензия.

### WIP / планируется

- [ ] Аутентификация и роли для `/control`.
- [ ] Поддержка нескольких именованных background-профилей.
- [ ] Управление фоном и сценами через WebSocket без polling.
- [ ] Настройки crop/contain и позиционирования фонового изображения.
- [ ] Метрики Prometheus и health dashboard.
- [ ] Docker/Compose-образ для чистой установки.
- [ ] Полноценный набор unit и integration тестов в CI.
- [ ] Ручной выбор beatmap/skin из панели управления.

## Архитектура

- `app.py` — aiohttp-сервер, очередь, producer и API.
- `static/player.html` — страница для OBS.
- `static/control.html` — панель управления.
- `static/fallback.svg` — постоянный фон по умолчанию.
- `danser_renderer.py` — локальный renderer.
- `discord_ingest.py` — Discord adapter.
- `submission_store.py` — SQLite-хранилище Discord submissions.
- `inbox/` — входящие `.osr`.
- `video_queue/` — готовые MP4.
- `archive/` — архив проигранных видео, если `DELETE_AFTER_PLAY=false`.
- `work/`, `rejected/`, `logs/` — временные файлы, отклонённые replay и логи.

## Требования

- Linux x86_64.
- Python 3.12+ (проверено на Python 3.14.8).
- `ffprobe`/FFmpeg.
- Для o!rdr: доступ в интернет и API key для нормального production rate limit.
- Для danser: GPU/OpenGL и установленный локальный danser build.
- Для Discord intake: Discord application, bot token и права канала.

### Зависимости и библиотеки

Прямые зависимости из `requirements.txt`:

```text
aiohttp>=3.13,<4
python-socketio[asyncio_client]>=5.14,<6
discord.py>=2.6,<3
```

- `aiohttp` — HTTP-сервер, multipart upload и HTTP-клиент для внешних API.
- `python-socketio[asyncio_client]` — Socket.IO-клиент для o!rdr.
- `discord.py` — Discord bot и slash-команды.
- `sqlite3` — встроенная библиотека Python для `submissions.db`.
- `asyncio`, `argparse`, `dataclasses`, `hashlib`, `json`, `logging`, `mimetypes`, `pathlib`, `re`, `shutil`, `signal`, `struct`, `time`, `uuid`, `zipfile` и прочие импорты проекта — стандартная библиотека Python.

Фактический набор пакетов в проверенном `venv`:

```text
aiohappyeyeballs==2.7.1
aiohttp==3.14.5
aiosignal==1.4.0
attrs==26.1.0
audioop-lts==0.2.2
bidict==0.24.1
certifi==2026.10.23
charset-normalizer==3.5.2
discord.py==2.7.1
frozenlist==1.8.0
h11==0.16.0
idna==3.20
multidict==7.2.1
propcache==0.5.4
python-engineio==4.15.1
python-socketio==5.17.1
requests==2.34.2
simple-websocket==1.1.0
urllib3==2.8.0
websocket-client==1.9.2
wsproto==1.3.2
yarl==1.25.1
```

Системные и runtime-зависимости: `bash`, `curl`, `unzip`, `ffmpeg`, `ffprobe`, SQLite, OpenGL/SDL3, GPU-драйвер с VAAPI (для аппаратного кодирования), современный браузер и OBS Studio. Для меню `configure.sh` нужен `whiptail` или `dialog`. Интернет нужен для o!rdr, osuokayu, beatmap mirror и Discord Gateway. Скрипт `install-danser.sh` устанавливает danser-go `0.12.1`, проверяет SHA-256 и размещает его библиотеки и `assets.dpak` в `tools/danser/`. В локальный danser-пакет входят `libdanser-core.so`, `libSDL3.so`, `libyuv.so`, `libbass.so`, `libbass_fx.so`, `libbassmix.so`, а также bundled FFmpeg (`libavcodec`, `libavformat`, `libavutil`, `libavfilter`, `libavdevice`, `libswscale`, `libswresample`) и бинарники `ffmpeg`/`ffprobe`.

## Настройка и запуск

```bash
cd /path/to/osu-stream-bot
./configure.sh
```

`configure.sh` открывает англоязычное терминальное меню со всеми параметрами и
подсказками, проверяет введённые значения и безопасно записывает `.env`.
API-ключ o!rdr и Discord token в меню скрыты. При сохранении предыдущая версия
остаётся в игнорируемом Git файле `.env.bak`. Если TUI не нужен, `.env`
по-прежнему можно создать вручную командой `cp .env.example .env`.

Параметры по умолчанию позволяют проверить веб-интерфейс без реального рендера:

```env
RENDER_BACKEND=ordr
ORDR_DEVELOPER_MODE=true
```

Для o!rdr production укажите ключ:

```env
RENDER_BACKEND=ordr
ORDR_DEVELOPER_MODE=false
ORDR_API_KEY=your_key_here
```

Для локального renderer:

```bash
./install-danser.sh
```

```env
RENDER_BACKEND=danser
DANSER_DIR=tools/danser
DANSER_SETTINGS=stream
DANSER_SKIN=default
```

Проверка и запуск:

```bash
./check.sh
./run.sh
```

Проверить только player без producer:

```bash
./run.sh --no-producer
```

Открыть страницы можно командами `./open-player.sh` и `./open-control.sh`, либо вручную:

- Player: `http://127.0.0.1:8787/`
- Control: `http://127.0.0.1:8787/control`
- Health: `http://127.0.0.1:8787/health`

## OBS

Добавьте **Browser Source**:

- URL: `http://127.0.0.1:8787/`
- Width: `1280`
- Height: `720`
- FPS: `60`
- включите audio для Browser Source, если OBS не включил его автоматически.

Не включайте `Shutdown source when not visible`, если очередь должна продолжаться при смене сцен.

## Фон в `/control`

В блоке **Waiting background** выберите картинку и нажмите **Set temporary background**. Она будет показана только когда player ожидает следующий replay. Ограничение размера задаётся `MAX_BACKGROUND_MIB` (по умолчанию 20 MiB). Файл не записывается ни в проект, ни в `state.json`, поэтому при остановке/перезапуске процесса он исчезает. Кнопка **Restore fallback.svg** сбрасывает его сразу.

HTTP API:

- `GET /api/background` — текущий статус.
- `POST /api/background` — multipart-поле `background`.
- `DELETE /api/background` — возврат к `static/fallback.svg`.
- `GET /background` — текущий image response для player.

Сервис по умолчанию слушает только localhost. Не публикуйте `/control` в интернет без reverse proxy и аутентификации.

## Discord

Создайте приложение в Discord Developer Portal, добавьте bot и пригласите его со scope `bot` и `applications.commands`. Нужны права канала: View Channel, Send Messages, Embed Links, Read Message History. В `.env` задайте:

```env
DISCORD_ENABLED=true
DISCORD_TOKEN=put_the_bot_token_here
DISCORD_GUILD_ID=123456789012345678
DISCORD_CHANNEL_ID=123456789012345678
DISCORD_QUEUE_LIMIT=50
DISCORD_USER_COOLDOWN_SECONDS=300
DISCORD_MAX_FILE_MIB=10
```

Доступные команды: `/submit replay:<file.osr>`, `/status submission_id:<id>`, `/queue`, `/cancel submission_id:<id>`. Токен нельзя коммитить или публиковать.

## Конфигурация

Для изменения настроек без ручного редактирования `.env` запустите
`./configure.sh`. Неинтерактивная проверка доступна через
`./configure.sh --validate`. Все параметры находятся в `.env.example`. Основные:

| Переменная | По умолчанию | Назначение |
|---|---:|---|
| `RENDER_BACKEND` | `ordr` | `ordr` или `danser` |
| `BUFFER_SIZE` | `2` | Целевой размер буфера |
| `DELETE_AFTER_PLAY` | `true` | Удалять MP4 после показа |
| `PRODUCER_ENABLED` | `true` | Запускать producer |
| `WEB_HOST` / `WEB_PORT` | `127.0.0.1 / 8787` | HTTP-сервер |
| `MAX_UPLOAD_MIB` | `20` | Лимит `.osr` upload |
| `MAX_BACKGROUND_MIB` | `20` | Лимит временного фона |
| `PLAYER_VOLUME` | `20` | Громкость при первом запуске |

## Тесты и проверка

В проекте пока нет отдельного pytest-набора; доступна smoke-проверка конфигурации и синтаксиса:

```bash
./check.sh
```

Она компилирует Python-модули, проверяет обязательные файлы, renderer, `ffprobe` и настройки. Для ручной проверки player используйте `./run.sh --no-producer`, откройте `/control`, загрузите фон, убедитесь, что он виден на `/`, затем перезапустите процесс и проверьте возврат `fallback.svg`.

## Проверенная конфигурация

Разработка и smoke-проверка проводились на:

- Arch Linux rolling, x86_64.
- CPU: Intel Xeon E5-2643 @ 3.30 GHz, 4 физических ядра / 8 потоков, 1 socket, Turbo до 3.50 GHz.
- RAM: 16 GiB установлено (около 15 GiB доступно ОС), swap 13 GiB.
- Тип RAM: определить автоматически не удалось — текущая среда не предоставляет данные SPD/DMI о поколении модулей, поэтому DDR-тип в README не заявляется.
- Python 3.14.8 в локальном `venv`.
- FFmpeg/ffprobe 9.0.2.
- AMD Radeon RX 550 (Baffin, VAAPI `/dev/dri/renderD128`) для danser hardware encoding.
- Разрешение рендера: 1280×720, 60 FPS.
- Python-библиотеки: aiohttp 3.14.5, python-socketio 5.17.1, python-engineio 4.15.1, discord.py 2.7.1 и зависимости из списка выше.
- danser-go 0.12.1, установленный через `install-danser.sh`.
- Локальный backend: `RENDER_BACKEND=danser`, аппаратный encoder `h264_vaapi`.

Другие Linux-дистрибутивы могут потребовать установки системных пакетов и настройки прав на GPU.

## Полезные команды

```bash
./configure.sh
./configure.sh --validate
./install-danser.sh
./run.sh --no-producer
./run.sh --recover-render 5094318
./open-player.sh
./open-control.sh
```

## Лицензия и сторонние сервисы

Проект распространяется по [MIT License](LICENSE): его можно свободно скачивать, использовать, изменять, распространять и включать в собственные проекты при сохранении текста лицензии и copyright notice.

osu!, o!rdr, danser, Discord и osuokayu — сторонние проекты/сервисы со своими условиями использования. Пользователь самостоятельно отвечает за соблюдение их правил, авторских прав на загружаемые изображения, beatmap и replay, а также за защиту Discord token/API keys.
