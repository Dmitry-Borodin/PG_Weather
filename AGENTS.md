# AGENTS — Операционные правила проекта

**Версия:** 1.3

## Структура проекта

```
AGENTS.md                          # Этот файл
.gitignore                         # reports/ исключены из git
.dockerignore                      # Исключения для Docker-сборки
Dockerfile                         # Образ: Python + Deno + Playwright
run.sh                             # Точка входа: Docker по умолчанию
requirements/
    weather-triage.md              # Требования к метео-триажу
    data-sources.md                # Источники данных + методы парсинга
prompts/
    weather-triage-prompt.md       # Промпт для LLM-ассистента
scripts/
    fetch_weather.py               # Сбор данных из открытых источников (v0.4)
    scraper.ts                     # Deno + Playwright: headless-скрапинг
    viewer_template.html           # Шаблон HTML-вьюера отчётов
reports/                           # Автогенерируемые отчёты (gitignored)
```

Вся логика и используемые данные должны быть явно отражены в веб-форме отчёта;


## Версионирование
- При обновлении требований или промптов повышать версию **внутри файла**.
- Версия должна соответствовать фактическим изменениям.

## Скрипты
- Docker (по умолчанию): `./run.sh [DATE] [LOCATIONS] [SOURCES] [HEADLESS_SOURCES]`
- Локальный (без headless): `./run.sh --local [DATE] [LOCATIONS] [SOURCES]`
- Дата по умолчанию — ближайшая суббота.
- Файлы отчётов: `YYYY-MM-DD_YYYYMMDD_HHMM.{json,md}` (forecast-date + timestamp).
- Python-часть не требует pip-зависимостей (только stdlib).
- Headless-скрапинг (Deno + Playwright) работает только из Docker.
- Скрипты не требуют платных API-ключей для базовой функциональности.

## Git
- **Агент НЕ коммитит в git** — только чтение (log, diff, status).
- Коммиты делает пользователь вручную.
