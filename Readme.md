# 🔍 Site Audit — комплексный аудит сайта

Инструмент для автоматического поиска технических проблем на сайте.
Скачивает страницы, анализирует HTML и формирует отчёт в Excel и HTML.

Два способа использования:
- **CLI** — запуск из командной строки
- **Telegram-бот** — запуск аудита через бота с inline-кнопками

---

## Что проверяется

| Проверка | Описание |
|---|---|
| **Пустые страницы** | Страницы с HTTP-ошибками, отсутствующим `<body>`, минимальным количеством видимого текста |
| **SEO** | Отсутствие или пустота `<title>`, `<meta description>`, `<link rel="canonical">`, `<h1>`, Open Graph-тегов, невалидный JSON-LD, noindex |
| **Битые ссылки** | Внутренние ссылки, ведущие на 4xx/5xx или несуществующие страницы. Опционально — внешние |
| **Картинки** | Битые изображения (не отвечают 200), тяжёлые (>500 КБ), устаревший формат (BMP/TIFF) |
| **Редиректы** | Цепочки из 2+ хопов, циклические редиректы, редиректы на внешние домены, HTTPS→HTTP даунгрейд |
| **Дубликаты** | Одинаковые Title, Description, H1 на разных URL, полные и частичные дубли контента, общий canonical |
| **Заглушки** | Lorem ipsum, TODO/FIXME в тексте и HTML-комментариях, типичные русские и английские заглушки |
| **robots.txt и Sitemap** | Наличие и валидность robots.txt, блокировка важных путей, директива Sitemap, URL из sitemap с ошибками или noindex |
| **Mixed Content** | HTTP-ресурсы (скрипты, стили, картинки, iframe) на HTTPS-страницах — активный и пассивный |
| **Страницы-сироты** | Страницы без входящих внутренних ссылок — невидимы для пользователей и плохо ранжируются |
| **Качество мета-тегов** | Длина title (<30 или >60 символов), длина description (<70 или >160), keyword stuffing, совпадение title и description |
| **Структура заголовков** | Множественные H1, пропущенные уровни (H1→H3 без H2), пустые и слишком длинные заголовки |

---

## Установка

### Требования

- Python 3.11+
- pip

### Зависимости

```bash
pip install requests beautifulsoup4 lxml openpyxl python-dotenv "python-telegram-bot[job-queue]" aiohttp[speedups]
```

### Конфигурация

Скопируйте файл `.env.example` в `.env` и заполните значения:

```bash
cp .env.example .env
```

Пустые строки и строки, начинающиеся с `#`, игнорируются. Включите прокси в `.env`:

PROXY_ENABLED=true
PROXY_FILE_PATH=./proxies.txt

Прокси ротируются по кругу (round-robin). Нерабочий прокси автоматически исключается после нескольких ошибок подряд и возвращается в пул через PROXY_COOLDOWN секунд.

Важно: добавьте proxies.txt в .gitignore — файл содержит учётные данные.

---

## Структура проекта

```
какая-то_папка/              ← запускать отсюда
├── .env                     # Переменные окружения (не коммитить!)
├── .env.example             # Шаблон переменных окружения
├── proxies.txt              # Список прокси-серверов (не коммитить!)
├── pyproject.toml           # Конфигурация проекта и инструментов
└── site_audit/
    ├── __init__.py
    ├── __main__.py          # CLI-оркестратор
    ├── utils.py             # Синхронный и асинхронный HTTP-клиент, хелперы
    ├── crawler.py           # Асинхронный Sitemap + обход по ссылкам
    ├── proxy.py             # Управление пулом прокси: ротация, cooldown, семафоры
    ├── report.py            # Генерация Excel и HTML
    ├── config/
    │   ├── __init__.py
    │   ├── settings.py      # Загрузка .env, валидация настроек
    │   └── logger.py        # JSON-логгер с ротацией
    ├── services/
    │   ├── __init__.py
    │   └── audit_service.py # Сервис аудита (единая точка входа)
    ├── bot/
    │   ├── __init__.py
    │   ├── __main__.py      # Точка входа: python -m site_audit.bot
    │   ├── app.py           # Сборка и запуск бота
    │   ├── handlers.py      # Обработчики команд и кнопок
    │   ├── keyboards.py     # Inline-клавиатуры
    │   └── states.py        # Состояние сессий пользователей
    └── checks/
        ├── __init__.py
        ├── empty_pages.py       # Пустые страницы
        ├── seo.py               # SEO-проверки
        ├── broken_links.py      # Битые ссылки
        ├── images.py            # Картинки
        ├── redirects.py         # Редиректы
        ├── duplicates.py        # Дубликаты
        ├── placeholders.py      # Заглушки
        ├── robots_sitemap.py    # robots.txt и валидация sitemap
        ├── mixed_content.py     # HTTP-ресурсы на HTTPS-страницах
        ├── orphan_pages.py      # Страницы-сироты
        ├── meta_quality.py      # Качество title и description
        └── heading_structure.py # Структура заголовков H1–H6
```

> **Важно:** запускать нужно из родительской папки, а не изнутри `site_audit/`.

---

## Использование: CLI

### Базовый запуск (все проверки)

```bash
python -m site_audit https://example.com
```

### Ограничить количество страниц

```bash
python -m site_audit https://example.com --limit 50
```

### Выбрать конкретные проверки

```bash
python -m site_audit https://example.com --checks seo,empty_pages,broken_links,meta_quality,heading_structure
```

### Проверить также внешние ссылки

```bash
python -m site_audit https://example.com --check-external-links
```

### Увеличить параллельность

```bash
python -m site_audit https://example.com --workers 20
```

### Сохранить отчёт в отдельную папку

```bash
python -m site_audit https://example.com --output-dir ./reports
```

### Показать список всех проверок

```bash
python -m site_audit --list-checks
```

Приоритет параметров: аргумент CLI перекрывает значение из `.env`, значение из `.env` перекрывает значение по умолчанию.

---

## Использование: Telegram-бот

### Запуск бота

```bash
python -m site_audit.bot
```

### Как работает бот

1. Отправьте `/start` — бот покажет приветствие и кнопку «Начать аудит»
2. Нажмите «Начать аудит» — бот попросит ввести URL сайта
3. Отправьте URL (например, `https://example.com`) — появится главное меню:
   - **Проверки** — включение/выключение отдельных проверок (toggle-кнопки)
   - **Настройки** — изменение параметров (лимит страниц, потоки, таймаут и др.)
   - **Запустить аудит** — запуск проверки
   - **Отмена** — сброс сессии
4. После запуска бот отправляет прогресс выполнения
5. По завершении бот отправит:
   - Текстовую сводку с результатами по каждой проверке
   - Excel-файл отчёта
   - HTML-файл отчёта

### Команды бота

| Команда | Описание |
|---|---|
| `/start` | Начать работу / сбросить сессию |
| `/help` | Справка по использованию |

### Ограничение доступа

Чтобы разрешить доступ к боту только определённым пользователям, укажите их Telegram ID в `.env`:

```bash
ALLOWED_USER_IDS=123456789,987654321
```

Если переменная пуста — доступ есть у всех.

---

## Параметры командной строки

| Параметр | По умолчанию | Описание |
|---|---|---|
| `base_url` | (обязательный) | URL сайта для аудита |
| `--checks` | все | Список проверок через запятую |
| `--list-checks` | — | Показать доступные проверки и выйти |
| `--max-crawl-pages` | 500 | Лимит страниц при обходе по ссылкам (если нет sitemap) |
| `--max-depth` | 3 | Максимальная глубина обхода |
| `--limit` | без лимита | Ограничить общее число URL для проверки |
| `--workers` | 10 | Количество параллельных потоков |
| `--delay` | 0.0 | Задержка между запросами (сек) |
| `--timeout` | 30 | Таймаут HTTP-запросов (сек) |
| `--min-text-length` | 100 | Порог пустой страницы (символов видимого текста) |
| `--max-image-size-kb` | 500 | Порог тяжёлой картинки (КБ) |
| `--check-external-links` | false | Проверять внешние ссылки |
| `--output-dir` | `./reports` | Директория для отчётов |
| `--excel-name` | `audit_report.xlsx` | Имя Excel-файла |
| `--html-name` | `audit_report.html` | Имя HTML-файла |
| `--no-proxy` | false | Отключить прокси для этого запуска |
| `--quiet` | false | Минимальный вывод в консоль |

---

## Переменные окружения (.env)

| Переменная | По умолчанию | Описание |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | (обязательная) | Токен бота от @BotFather |
| `ALLOWED_USER_IDS` | пусто (все) | Telegram ID через запятую |
| `PROXY_ENABLED` | false | Включить использование прокси |
| `PROXY_FILE_PATH` | `./proxies.txt` | Путь к файлу со списком прокси |
| `PROXY_COOLDOWN` | 120 | Время исключения нерабочего прокси из ротации (сек) |
| `PROXY_MAX_FAILS` | 3 | Количество ошибок подряд до отключения прокси |
| `PROXY_MAX_CONNECTIONS` | 5 | Максимум одновременных соединений на один прокси |
| `DEFAULT_MAX_CRAWL_PAGES` | 500 | Лимит страниц при BFS-обходе |
| `DEFAULT_MAX_DEPTH` | 3 | Глубина обхода |
| `DEFAULT_LIMIT` | 0 (без лимита) | Лимит URL для проверки |
| `DEFAULT_WORKERS` | 10 | Потоки загрузки |
| `DEFAULT_DELAY` | 0.0 | Задержка между запросами (сек) |
| `DEFAULT_TIMEOUT` | 30 | Таймаут запросов (сек) |
| `DEFAULT_MIN_TEXT_LENGTH` | 100 | Порог пустой страницы (символов) |
| `DEFAULT_MAX_IMAGE_SIZE_KB` | 500 | Порог тяжёлой картинки (КБ) |
| `DEFAULT_CHECK_EXTERNAL_LINKS` | false | Проверять внешние ссылки |
| `OUTPUT_DIR` | `./reports` | Директория для отчётов |
| `EXCEL_REPORT_NAME` | `audit_report.xlsx` | Имя Excel-файла |
| `HTML_REPORT_NAME` | `audit_report.html` | Имя HTML-файла |
| `LOG_LEVEL` | `INFO` | Уровень логирования |
| `LOG_FILE_PATH` | `./logs/app.log` | Путь к файлу логов |
| `LOG_MAX_BYTES` | 10485760 | Макс. размер лога (байт) |
| `LOG_BACKUP_COUNT` | 5 | Кол-во файлов ротации |

---

## Отчёты

После завершения аудита создаются два файла:

### Excel (`audit_report.xlsx`)

- Лист «Сводка» — общая статистика: сайт, дата, количество проблем по каждой проверке
- Отдельный лист для каждой проверки — таблица с проблемами, автоширина колонок, фильтры в шапке, закреплённая первая строка
- Удобно открывать в Excel или Google Sheets для фильтрации и сортировки.

### HTML (`audit_report.html`)

- Единый файл с навигацией по проверкам
- Таблицы с кликабельными ссылками
- Подходит для отправки клиенту или просмотра в браузере

---

## Логирование

Логи выводятся в двух форматах одновременно:

- **Консоль (stdout)** — читаемый формат с цветовой подсветкой уровней
- **Файл (`logs/app.log`)** — JSON-формат для парсинга и мониторинга

Каждая запись в файле содержит поля: `timestamp`, `level`, `logger`, `message`, `trace_id`, `context`.

Папка `logs/` создаётся автоматически при запуске. Ротация файлов настраивается через `LOG_MAX_BYTES` и `LOG_BACKUP_COUNT`.

---

## Как это работает

```
1. Сбор URL
   ├── Асинхронно пробует скачать sitemap.xml / sitemap_index.xml
   ├── Вложенные sitemap загружаются параллельно
   └── Если sitemap нет → асинхронный BFS-обход по внутренним ссылкам

2. Загрузка страниц
   └── Асинхронное скачивание через aiohttp (до 100 одновременных соединений)
    └── Опционально — через пул прокси с round-robin ротацией

3. Проверки
   ├── Каждая проверка получает уже скачанные страницы
   ├── Не делает повторных запросов к уже загруженным URL
   ├── CPU-bound проверки (SEO, дубликаты, заглушки, mixed content,
   │   мета-теги, заголовки, сироты) — работают с HTML в памяти
   └── IO-bound проверки (картинки, ссылки, редиректы, robots.txt) — асинхронные
       └── Используют тот же прокси-пул и таймаут, что и загрузка страниц

4. Отчёты
   └── Генерация Excel + HTML из результатов всех проверок
```

---

## Примеры

### CLI: быстрый аудит небольшого сайта

```bash
python -m site_audit https://mysite.ru --limit 30
```

### CLI: полный аудит с внешними ссылками

```bash
python -m site_audit https://mysite.ru \
    --workers 3 \
    --delay 1.0 \
    --timeout 30
```

### CLI: только новые проверки (robots, mixed content, мета-теги, заголовки, сироты)

```bash
python -m site_audit https://mysite.ru \
    --checks robots_sitemap,mixed_content,meta_quality,heading_structure,orphan_pages \
    --limit 100
```

### CLI: аудит через прокси (для сайтов с блокировкой по IP)

Убедитесь, что в `.env` указано `PROXY_ENABLED=true` и файл `proxies.txt` заполнен.
Прокси используются на всех этапах: загрузка страниц, проверка ссылок, проверка картинок, проверка редиректов.

```bash
python -m site_audit https://mysite.ru \
    --workers 2 \
    --delay 0.5 \
    --timeout 90
```

### CLI: аудит без прокси (разовое отключение)

python -m site_audit https://mysite.ru --no-proxy

### CLI: только SEO и дубликаты на первых 100 страницах

```bash
python -m site_audit https://mysite.ru \
    --checks seo,duplicates \
    --limit 100
```

### CLI: аудит с задержкой (для сайтов с защитой от ботов)

```bash
python -m site_audit https://mysite.ru \
    --workers 3 \
    --delay 1.0 \
    --timeout 30
```

### Telegram-бот: запуск

```bash
python -m site_audit.bot
```

---

## Использование как библиотеки

```python
from site_audit.config import get_settings, setup_logging
from site_audit.services import AuditService
from site_audit.services.audit_service import AuditParams

# Инициализация
settings = get_settings()
setup_logging(log_level=settings.log_level, log_file_path=settings.log_file_path)

# Создание сервиса
service = AuditService(settings)

# Синхронный запуск (обёртка над asyncio.run)
params = service.create_params_from_settings("https://example.com")
result = service.run_audit(params)

print(f"Проблем найдено: {result.total_issues}")
print(f"Excel: {result.excel_path}")
print(f"HTML: {result.html_path}")
```

# Асинхронный запуск (рекомендуется)
import asyncio
from site_audit.config import get_settings, setup_logging
from site_audit.services import AuditService

settings = get_settings()
setup_logging(log_level=settings.log_level, log_file_path=settings.log_file_path)

service = AuditService(settings)
params = service.create_params_from_settings("https://example.com")

async def main():
    result = await service.run_audit_async(params)
    print(f"Проблем найдено: {result.total_issues}")

asyncio.run(main())


# Низкоуровневое использование (без сервиса)
import asyncio
from site_audit.crawler import async_try_sitemap
from site_audit.checks import seo, empty_pages, broken_links
from site_audit.utils import fetch, parse_html, visible_text

# Собрать URL (асинхронно)
urls = asyncio.run(async_try_sitemap("https://example.com"))

# Проверить одну страницу (синхронно)
result = seo.check("https://example.com/about")
print(result["issues"])

# Пакетная проверка
pages = [{"url": u, "resp": None, "html": None} for u in urls]
seo_results = seo.check_many(pages)
for r in seo.filter_with_issues(seo_results):
    print(r["url"], r["issues"])


---

### VPS

- перечитать конфигурацию systemd
systemctl daemon-reload

- включить автозапуск при загрузке И сразу запустить
systemctl enable --now seo-bot.service

- проверить, что служба работает (Active: active (running))
systemctl status seo-bot.service

- смотреть живые логи бота
journalctl -u seo-bot.service -f

---

## Лицензия

MIT
