# osu-stream-bot Wiki & Complete Reference (RU)

Полная техническая документация и справочник по архитектуре, настройке и эксплуатации проекта `osu-stream-bot`.

---

## Оглавление

1. [Обзор архитектуры и компоненты](#1-обзор-архитектуры-и-компоненты)
2. [Режимы работы (RUN_MODE)](#2-режимы-работы-run_mode)
3. [Развертывание на двух VPS (Main Controller + Render Worker)](#3-развертывание-на-двух-vps)
4. [Безопасность и hardening](#4-безопасность-и-hardening)
5. [Конфигурация (Все переменные окружения)](#5-конфигурация-все-переменные-окружения)
6. [Интеграция с Discord](#6-интеграция-с-discord)
7. [Настройка OBS Studio](#7-настройка-obs-studio)
8. [Управление кэшем и хранилищем](#8-управление-кэшем-и-хранилищем)
9. [Диагностика и решение проблем](#9-диагностика-и-решение-проблем)

---

## 1. Обзор архитектуры и компоненты

Проект предназначен для организации круглосуточного стрима osu!standard реплеев на Twitch / YouTube. Система может работать как единый монолит (standalone), так и распределенно (контроллер + выделенный воркер рендера).

```
                  [ Twitch / YouTube RTMP ]
                       ▲             ▲
      (Zero-CPU Native)│             │ (Legacy Browser Source)
      ┌────────────────┴─────────────┴─────────────────┐
      │              Main Controller VPS               │
      │ ┌──────────────────────┐  ┌──────────────────┐ │
      │ │ stream_broadcaster   │  │  Xvfb + Headless │ │
      │ │ (FFmpeg -c copy)     │  │    OBS Studio    │ │
      │ └──────────▲───────────┘  └────────▲─────────┘ │
      │            │                       │           │
      │            └───────────┬───────────┘           │
      │                        │ Local API / Browser   │
      │              ┌─────────┴─────────┐             │
      │              │ Web Player :8787  │             │
      │              └─────────▲─────────┘             │
      │              ┌─────────┴─────────┐             │
      │              │ Render API :8790  │             │
      │              └─────────▲─────────┘             │
      └────────────────────────┼───────────────────────┘
                               │ WireGuard VPN (10.10.0.0/24)
                               │ Bearer Token + Lease Auth
                  ┌────────────┴────────────┐
                  │    Render Worker VPS    │
                  │ ┌─────────────────────┐ │
                  │ │  render_worker.py   │ │
                  │ └──────────▲──────────┘ │
                  │ ┌──────────┴──────────┐ │
                  │ │ danser-go (CPU/GPU) │ │
                  │ └─────────────────────┘ │
                  └─────────────────────────┘
```

### Ключевые модули:
- **`app.py`**: Главный монолит и точка входа контроллера. Запускает aiohttp веб-сервер, веб-плеер (`/`), админ-панель (`/control`), фоновый Producer и фоновый наблюдатель завершенных задач.
- **`stream_broadcaster.py`**: Автономный 24/7 RTMP вещатель (Twitch + YouTube). Потребляет <0.5% CPU благодаря `-c copy` без транскодирования, автоматически переключается на `tools/standby.mp4` при пустой очереди и полностью заменяет тяжёлый OBS на сервере.
- **`config.py`**: Валидация переменных окружения, fail-closed проверки для ролей и структуры папок.
- **`queue_store.py`**: Управление очередью готовых MP4 (`video_queue/`), буферизацией, состоянием плеера (`state.json`), а также директориями `inbox/`, `work/`, `archive/`, `rejected/`.
- **`render_job_store.py`**: Асинхронное SQLite-хранилище задач рендера (`render_jobs.db`). Обеспечивает атомарный захват (`claim_job`), управление арендой (`lease_token`, `lease_expires_at`), heartbeat и архивацию.
- **`render_api.py`**: Внутренний REST API контроллера для воркеров (запуск на отдельном сетевом интерфейсе и порту).
- **`render_worker.py`**: Легковесный фоновый демон воркера. Забирает задачи по HTTP, скачивает реплей, рендерит в danser-go, загружает MP4 стримом и подтверждает завершение.
- **`discord_ingest.py`**: Discord-бот для приема реплеев от зрителей через слэш-команды.
- **`danser_renderer.py`**: Локальный движок рендера danser-go и менеджер библиотеки карт `BeatmapLibrary` с автоматическим скачиванием и защищенным LRU-кэшем.

---

## 2. Режимы работы (RUN_MODE)

- **`standalone`** (по умолчанию): Всё в одном процессе. Локально рендерит через `danser-go` или `o!rdr`, воспроизводит видео и управляет очередью. Идеально для одного мощного ПК или монолитного сервера.
- **`controller`**: Главный узел. Ведет базу задач, раздает задачи воркерам через `render_api.py`, принимает готовые MP4, валидирует их через `ffprobe` и передает в веб-плеер и вещателю. Локальный danser не запускается.
- **`render-worker`**: Выделенный узел рендера (CPU/GPU). Не поднимает веб-сервер, непрерывно опрашивает контроллер, рендерит реплеи и отправляет артефакты обратно.
- **`broadcaster`**: Режим для отдельной выделенной ноды вещания (если стриминг вынесен на отдельный сервер). При стандартной схеме с двумя серверами на контроллере остаётся `RUN_MODE=controller`, а вещатель запускается параллельно как клиент через `./run-broadcaster.sh` или `stream-broadcaster.service`.

---

## 3. Развертывание на двух VPS

### Шаг 1. Настройка приватной сети (WireGuard)
Контроллер и воркер связываются через приватный VPN-туннель. Render API **никогда** не должен смотреть в открытый интернет:
- Controller VPN IP: `10.10.0.1/24`
- Worker VPN IP: `10.10.0.2/24`
Проверка: `ping 10.10.0.1` с воркера.

### Шаг 2. Установка Controller VPS
```bash
sudo ./deploy/install.sh controller
```
Скрипт установит Python-окружение, `ffmpeg`, `nginx`, `apache2-utils`, создаст пользователя `osu-stream` и установит systemd-юниты.

Настройте `/etc/osu-stream/controller.env`:
```env
RUN_MODE=controller
RENDER_API_HOST=10.10.0.1
RENDER_API_PORT=8790
RENDER_API_TOKEN=<сгенерированный_токен_openssl_rand_-hex_32>

# Настройки прямого RTMP вещания (Twitch и YouTube)
STREAM_TWITCH_KEY=live_...
STREAM_YOUTUBE_KEY=xxxx-...
STREAM_API_URL=http://127.0.0.1:8787
```

**Выбор вещателя на Controller VPS:**
- **Рекомендуемый (Headless Native Broadcaster, <0.5% CPU)**:
  ```bash
  sudo systemctl enable --now osu-stream-controller stream-broadcaster
  ```
  *OBS Studio и виртуальный дисплей Xvfb не требуются! Вещатель работает непрерывно 24/7.*
- **Классический (OBS Studio с Browser Source)**:
  ```bash
  sudo systemctl enable --now xvfb osu-stream-controller obs
  ```

### Шаг 3. Установка Render Worker VPS
```bash
sudo ./deploy/install.sh worker
```
Скрипт установит Mesa/OpenGL, `ffmpeg`, `danser-go 0.12.1`, подготовит директории.

Настройте `/etc/osu-stream/worker.env`:
```env
RUN_MODE=render-worker
CONTROLLER_RENDER_URL=http://10.10.0.1:8790
RENDER_API_TOKEN=<тот_же_самый_токен>
DANSER_SETTINGS=stream-cpu
```

Синхронизируйте карты osu! в `tools/danser/songs/` и запустите воркер:
```bash
sudo systemctl start osu-stream-worker
```

---

## 4. Безопасность и hardening

1. **HTTPS и Nginx Reverse Proxy**:
   Панель `/control` и API защищены HTTP Basic Auth с bcrypt (`htpasswd -cB -C 12`). Плеер `/` доступен только с `127.0.0.1` для OBS. Все HTTP-запросы принудительно перенаправляются на HTTPS (код 308). Исходный заголовок `Authorization: Basic` удаляется перед проксированием в aiohttp.
2. **Systemd Sandboxing**:
   - `ProtectSystem=strict`
   - `ProtectHome=true` (для OBS `ProtectHome=read-only` с изоляцией профиля)
   - `NoNewPrivileges=true`
   - `UMask=0077` (все создаваемые файлы доступны только сервисному пользователю)
   - `CapabilityBoundingSet=` и `AmbientCapabilities=` очищены.
3. **Безопасность Xvfb**:
   Виртуальный дисплей запускается с флагом `-nolisten tcp` и кукой авторизации через `xauth` (файл куки в защищенном `RuntimeDirectory=osu-stream-xvfb`).
4. **Стриминг-ключ Twitch**:
   Ключ трансляции хранится в профиле OBS (`~osu-stream/.config/obs-studio/basic/profiles/`) в файле `service.json` с правами `chmod 600`. Не размещайте ключ в git или открытых скриптах.

---

## 5. Конфигурация (Все переменные окружения)

| Переменная | По умолчанию | Описание |
|---|---|---|
| `RUN_MODE` | `standalone` | Режим работы: `standalone`, `controller`, `render-worker` |
| `RENDER_BACKEND` | `ordr` | Бэкенд в standalone: `ordr` или `danser` |
| `BUFFER_SIZE` | `2` | Целевой размер буфера готовых видео + задач в обработке |
| `DELETE_AFTER_PLAY` | `true` | Удалять MP4 после окончания воспроизведения |
| `PRODUCER_ENABLED` | `true` | Включить автоматическое наполнение очереди |
| `WEB_HOST` / `WEB_PORT` | `127.0.0.1:8787` | Адрес и порт веб-плеера и панели управления |
| `RENDER_API_HOST` | `127.0.0.1` | IP-адрес для привязки Render API (задавайте VPN IP) |
| `RENDER_API_PORT` | `8788` | Порт Render API на контроллере |
| `RENDER_API_TOKEN` | *(пусто)* | Секретный Bearer-токен авторизации воркеров |
| `CONTROLLER_RENDER_URL` | *(пусто)* | URL контроллера для воркера (`http://<VPN_IP>:8790`) |
| `WORKER_ID` | `worker-<hostname>` | Уникальный идентификатор воркера |
| `RENDER_HEARTBEAT_SECONDS`| `15.0` | Интервал отправки heartbeat воркером (1..20 сек) |
| `MAX_ARTIFACT_MIB` | `1024` | Максимальный размер загружаемого MP4 артефакта |
| `DANSER_DIR` | `tools/danser` | Путь к директории danser-go |
| `DANSER_SETTINGS` | `stream-cpu` | Профиль настроек danser (`stream-cpu`, `stream`, `stream-amf`) |
| `DANSER_MAX_SONGS_MIB` | `5120` | Лимит размера кэша карт в Songs (LRU-очистка) |
| `DANSER_RENDER_TIMEOUT_SECONDS`| `3600` | Таймаут процесса рендера одной карты |
| `PLAYER_VOLUME` | `20` | Начальная громкость воспроизведения (0..100) |
| `DISCORD_ENABLED` | `false` | Включение Discord-бота |
| `STREAM_TWITCH_KEY` | *(пусто)* | Ключ трансляции Twitch для прямого вещателя |
| `STREAM_TWITCH_URL` | `rtmp://live.twitch.tv/app/` | Сервер ingest Twitch |
| `STREAM_YOUTUBE_KEY`| *(пусто)* | Ключ трансляции YouTube Live |
| `STREAM_YOUTUBE_URL`| `rtmp://a.rtmp.youtube.com/live2/` | Сервер ingest YouTube |
| `STREAM_CUSTOM_RTMP_URL` | *(пусто)* | Дополнительный RTMP URL назначения |
| `STREAM_API_URL` | `http://127.0.0.1:8787` | Адрес API контроллера для вещателя |
| `STREAM_STANDBY_PATH`| `tools/standby.mp4` | Путь к 60 FPS видео-заглушке ожидания |
| `STREAM_POLL_INTERVAL_SECONDS` | `1.0` | Интервал проверки статуса при вещании (сек) |
| `STREAM_DRY_RUN` | `false` | Тестовый режим без передачи пакетов в RTMP |

---

## 6. Интеграция с Discord

Для включения Discord intake:
1. Создайте бота на [Discord Developer Portal](https://discord.com/developers/applications).
2. Выдайте боту разрешение `applications.commands` (привилегированный Message Content Intent **не требуется**).
3. В `.env` укажите:
   ```env
   DISCORD_ENABLED=true
   DISCORD_TOKEN=ваш_токен
   DISCORD_GUILD_ID=ид_сервера
   DISCORD_CHANNEL_ID=ид_канала
   ```
4. Доступные слэш-команды:
   - `/submit replay:<file.osr>` — отправка реплея на стрим.
   - `/status` (или `/queue`) — текущее состояние очереди и статус реплея пользователя.
   - `/cancel submission_id:<id>` — отмена своей заявки.

---

## 7. Вещание в RTMP: Stream Broadcaster vs OBS Studio

### Вариант 1: Headless Stream Broadcaster (Рекомендуется для VPS)
Легковесный вещатель через FFmpeg напрямую отправляет реплеи в RTMP без перекодирования (`-c copy`):
- **Нагрузка на процессор**: менее 0.5% одного CPU-ядра.
- **Требования**: не требует графической подсистемы, Xvfb, OpenGL и GPU на контроллере.
- **Поддержка рестрима**: одновременная параллельная отправка на Twitch и YouTube.
- **Бесшовная заглушка**: при пустой очереди крутит `tools/standby.mp4` без разрыва RTMP-сессии.

**Ручной запуск:**
```bash
./run-broadcaster.sh
# Или запуск проверки без стрима:
./run-broadcaster.sh --dry-run
```

**Systemd сервис:**
```bash
sudo systemctl enable --now stream-broadcaster
```

### Вариант 2: OBS Studio с Browser Source (Классический режим)
Подходит, если вам необходим сложный визуальный оверлей, чат или донат-виджеты:
1. **Browser Source в OBS**:
   - URL: `http://127.0.0.1:8787/`
   - Разрешение: `1280x720` (или `1920x1080`)
   - Включите: «Управлять звуком с помощью OBS» и «Загружать заново страницу, если источник становится активным».
2. **Headless запуск на сервере**:
   Юнит `obs.service` запускает OBS Studio через программный OpenGL драйвер llvmpipe (`xvfb.service + obs.service`). Требует ~1.5–2.5 ядра CPU для рендеринга сцены.

---

## 8. Управление кэшем и хранилищем

- **LRU-кэш карт (`tools/danser/songs`)**:
  В standalone-режиме загрузчик карт автоматически следит за размером директории `songs/`. При превышении `DANSER_MAX_SONGS_MIB` (по умолчанию 5 ГБ) директории карт сортируются по времени последнего использования (`mtime`, обновляемый при каждом обращении) и старые наборы удаляются до отметки 80% от лимита. Текущая активная карта защищена от удаления.
- **Временный фон ожидания**:
  Через `/control` можно загрузить изображение ожидания (PNG, JPG, WebP, SVG, AVIF). Файл сохраняется только в оперативной памяти и сбрасывается к `static/fallback.svg` при перезапуске сервиса.

---

## 9. Диагностика и решение проблем

- **Проверка журналов**:
  ```bash
  sudo journalctl -u osu-stream-controller -f
  sudo journalctl -u osu-stream-worker -f
  sudo journalctl -u obs -f
  ```
- **Проверка статуса задач в БД контроллера**:
  ```bash
  sqlite3 render_jobs.db "SELECT id, status, worker_id, attempts FROM render_jobs ORDER BY created_at DESC LIMIT 5;"
  ```
- **Воркер не запускается с ошибкой beatmaps**:
  Проверьте, что в `tools/danser/songs` есть хотя бы один файл `.osu`. Скрипт валидации работает в режиме fail-closed.
- **Сброс зависшей задачи рендера**:
  Контроллер автоматически возвращает протухшие задачи (stale lease) в очередь `pending` при каждом запуске и периодически в рантайме.
