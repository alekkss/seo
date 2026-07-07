# 🔍 Site Audit — комплексный аудит сайта

Инструмент командной строки для автоматического поиска технических проблем на сайте.
Скачивает страницы, анализирует HTML и формирует отчёт в Excel и HTML.

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

---

## Установка

### Требования

- Python 3.10+
- pip

### Зависимости

```bash
pip install requests beautifulsoup4 lxml openpyxl
```

### Структура проекта

```
какая-то_папка/              ← запускать отсюда
└── site_audit/
    ├── __init__.py
    ├── __main__.py          # CLI-оркестратор
    ├── utils.py             # HTTP-клиент, хелперы
    ├── crawler.py           # Sitemap + обход по ссылкам
    ├── report.py            # Генерация Excel и HTML
    └── checks/
        ├── __init__.py
        ├── empty_pages.py   # Пустые страницы
        ├── seo.py           # SEO-проверки
        ├── broken_links.py  # Битые ссылки
        ├── images.py        # Картинки
        ├── redirects.py     # Редиректы
        ├── duplicates.py    # Дубликаты
        └── placeholders.py  # Заглушки
```

> **Важно:** запускать нужно из родительской папки, а не изнутри `site_audit/`.

---

## Использование

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
python -m site_audit https://example.com --checks seo,empty_pages,broken_links
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
| `--timeout` | 15 | Таймаут HTTP-запросов (сек) |
| `--min-text-length` | 100 | Порог пустой страницы (символов видимого текста) |
| `--max-image-size-kb` | 500 | Порог тяжёлой картинки (КБ) |
| `--check-external-links` | false | Проверять внешние ссылки |
| `--output-dir` | . | Директория для отчётов |
| `--excel-name` | audit_report.xlsx | Имя Excel-файла |
| `--html-name` | audit_report.html | Имя HTML-файла |
| `--quiet` | false | Минимальный вывод в консоль |

---

## Отчёты

После завершения аудита создаются два файла:

### Excel (`audit_report.xlsx`)

- Лист «Сводка» — общая статистика: сайт, дата, количество проблем по каждой проверке
- Отдельный лист для каждой проверки — таблица с проблемами, автоширина колонок, фильтры в шапке, закреплённая первая строка

Удобно открывать в Excel или Google Sheets для фильтрации и сортировки.

### HTML (`audit_report.html`)

- Единый файл с навигацией по проверкам
- Таблицы с кликабельными ссылками
- Подходит для отправки клиенту или просмотра в браузере

---

## Как это работает

```
1. Сбор URL
   ├── Пробует скачать sitemap.xml / sitemap_index.xml
   └── Если sitemap нет → BFS-обход по внутренним ссылкам

2. Загрузка страниц
   └── Параллельное скачивание HTML всех найденных URL

3. Проверки
   ├── Каждая проверка получает уже скачанные страницы
   ├── Не делает повторных запросов к уже загруженным URL
   └── Дополнительные запросы только для внешних ресурсов
       (картинки, внешние ссылки, редиректы)

4. Отчёты
   └── Генерация Excel + HTML из результатов всех проверок
```

---

## Примеры

### Быстрый аудит небольшого сайта

```bash
python -m site_audit https://mysite.ru --limit 30
```

### Полный аудит с внешними ссылками и сохранением в папку

```bash
python -m site_audit https://mysite.ru \
    --check-external-links \
    --workers 15 \
    --output-dir ./audit_results
```

### Только SEO и дубликаты на первых 100 страницах

```bash
python -m site_audit https://mysite.ru \
    --checks seo,duplicates \
    --limit 100
```

### Аудит с задержкой (для сайтов с защитой от ботов)

```bash
python -m site_audit https://mysite.ru \
    --workers 3 \
    --delay 1.0 \
    --timeout 30
```

---

## Использование как библиотеки

```python
from site_audit.crawler import try_sitemap, crawl
from site_audit.checks import seo, empty_pages, broken_links
from site_audit.utils import fetch, parse_html, visible_text

# Собрать URL
urls = try_sitemap("https://example.com")

# Проверить одну страницу
result = seo.check("https://example.com/about")
print(result["issues"])

# Пакетная проверка
pages = [{"url": u, "resp": None, "html": None} for u in urls]
seo_results = seo.check_many(pages)
for r in seo.filter_with_issues(seo_results):
    print(r["url"], r["issues"])
```

---

## FAQ

**Q: Sitemap не найден, обход собирает мало страниц**
A: Увеличьте глубину и лимит: `--max-depth 5 --max-crawl-pages 1000`

**Q: Сайт блокирует запросы / много ошибок 403/429**
A: Уменьшите параллельность и добавьте задержку: `--workers 2 --delay 2.0`

**Q: Аудит занимает слишком много времени**
A: Ограничьте число страниц (`--limit 100`) или выберите конкретные проверки (`--checks seo,empty_pages`)

**Q: Как проверить только SEO без битых ссылок?**
A: `--checks seo`

**Q: Excel-файл не открывается**
A: Убедитесь, что установлен openpyxl: `pip install openpyxl`

---

## Лицензия

MIT
