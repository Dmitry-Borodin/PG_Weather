# AGENTS — Операционные правила проекта

**Версия:** 1.4

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
    priorities.md                  # Приоритеты скачивания и использования данных (v2.0)
    flyability.md                  # Оценка лётности: очки, флаги, статусы (v2.0)
prompts/
    weather-triage-prompt.md       # Промпт для LLM-ассистента
scripts/
    fetch_weather.py               # Оркестратор: CLI, LOCATIONS, assess_location, main (v2.0)
    fetchers.py                    # Сбор данных: fallback chains, API, ensemble, GeoSphere, MOSMIX
    analysis.py                    # Анализ: scoring, flags, per-model, thermal window, MP-интеграция
    report.py                      # Генерация отчётов: console, Markdown, HTML
    scraper.ts                     # Deno + Playwright: headless-скрапинг
    viewer_template.html           # Шаблон HTML-вьюера отчётов (v2.0)
reports/                           # Автогенерируемые отчёты (gitignored)
```

## Правила ассистента
- Вся логика и используемые данные должны быть явно отражены в веб-форме отчёта

- Надо всегда сохранять соответствие scripts и requirements. Если что-то изменилось в коде — надо обновить требования, и наоборот.

- Можно читать с гита, но не коммитить. Коммиты делает пользователь вручную.

- В конце ответа ассистент добавляет git commit сообщение (без команды, только сообщение) с кратким описанием изменений в отчёте (например, "Added Lenggries, Forecast timezone aware 

Added new flying spot Lenggries
Forecast time is now timezone dependent
Adjusted (decreased) column widths for hourly forecast table in Location details").


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
