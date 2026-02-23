# AGENTS — Операционные правила проекта

**Версия:** 1.2

## Структура проекта

```
AGENTS.md                          # Этот файл
.gitignore                         # reports/ исключены из git
.dockerignore                      # Исключения для Docker-сборки
Dockerfile                         # Образ: Python + Deno + Playwright
run.sh                             # Точка входа: ./run.sh / ./run.sh --docker
requirements/
    weather-triage.md              # Требования к метео-триажу
    data-sources.md                # Источники данных + методы парсинга
prompts/
    weather-triage-prompt.md       # Промпт для LLM-ассистента
scripts/
    fetch_weather.py               # Сбор данных из открытых источников
    scraper.ts                     # Deno + Playwright: headless-скрапинг
    viewer_template.html           # Шаблон HTML-вьюера отчётов
reports/                           # Автогенерируемые отчёты (gitignored)
```

## Версионирование
- При обновлении требований или промптов повышать версию **внутри файла**.
- Версия должна соответствовать фактическим изменениям.

## Скрипты
- Локальный запуск (без headless): `./run.sh YYYY-MM-DD [LOCATIONS] [SOURCES]`
- Docker запуск (с headless Playwright): `./run.sh --docker YYYY-MM-DD [LOCATIONS] [SOURCES] [HEADLESS_SOURCES]`
- Python-часть не требует pip-зависимостей (только stdlib).
- Headless-скрапинг (Deno + Playwright) работает только из Docker.
- Скрипты не требуют платных API-ключей для базовой функциональности.

## Git
- **Агент НЕ коммитит в git** — только чтение (log, diff, status).
- Коммиты делает пользователь вручную.
