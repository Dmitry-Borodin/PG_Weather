# Источники данных для метео-триажа

**Версия:** 4.1

---

## Модельный стек (v2.0 — fallback chains)

В v2.0 детерминистические модели организованы в **семейства** (families) с fallback chains.
Скрипт пробует модели в цепочке по порядку; как только получена — грубее не запрашиваются.

### Детерминистические семейства

| Семейство | Цепочка (fallback) | Эндпоинт | Роль |
|-----------|-------------------|----------|------|
| **ICON** | D2 2km → EU 7km → Global 13km | `/v1/dwd-icon?models=<model>` | Хай-рез для Альп; D2 только ≤48ч |
| **ECMWF** | IFS 0.25° → IFS 0.4° | `/v1/forecast?models=<model>` | Опорная глобальная модель |
| **GFS** | Seamless (одна модель) | `/v1/gfs?models=gfs_seamless` | Единственная с BL height, LI, CIN → нужна для W* |

### Ансамблевые модели

| Модель | Эндпоинт | Разрешение | Роль |
|--------|----------|------------|------|
| **ECMWF ENS** | `ensemble-api.open-meteo.com/v1/ensemble?models=ecmwf_ifs025` | 51 member | p10/p50/p90/spread |
| **ICON-EU EPS** | `ensemble-api.open-meteo.com/v1/ensemble?models=icon_eu` | 40 member | p10/p50/p90/spread |

### Региональные

| Модель | Эндпоинт | Разрешение | Роль |
|--------|----------|------------|------|
| **GeoSphere AROME** | `dataset.api.hub.geosphere.at/v1/timeseries/forecast/nwp-v1-1h-2500m` | 2.5 км | Хай-рез для Австрии, полный ряд (UTC timestamps) |
| **DWD MOSMIX** | `opendata.dwd.de/…/MOSMIX_L_LATEST_{station}.kmz` | station | Точечный sanity-check (только для локаций с `mosmix_id`) |

### Headless-источники (только Docker)

| Источник | Скрапер | Роль |
|----------|---------|------|
| **Meteo-Parapente** | `scraper.ts` → Playwright | Windgram / sounding (перехват XHR + DOM) |
| **XContest** | `scraper.ts` → Playwright | Реальные полёты рядом с локацией |
| **ALPTHERM** | `scraper.ts` → Playwright | Austro Control — обзорная карта термиков |

### Убрано

| Источник | Причина |
|----------|---------|
| ~~BrightSky @13 forecast~~ | Дублировал MOSMIX без pressure-level / CAPE |

### НЕ реализовано (упоминалось ранее)

| Источник | Статус |
|----------|--------|
| ~~GeoSphere TAWES observations~~ | Нет в коде. Станции (11320, 11121, 11130, …) не используются |
| ~~Автоматическое определение фёна~~ | Нет отдельной функции. Данные (RH700, wind700) доступны, логика не реализована |

---

## Слои данных (per source × location)

### at_13_local
Значения в **13:00 Europe/Berlin с учётом DST** (через `zoneinfo`).

### thermal_window_stats
Агрегация по окну **09:00–18:00 local**:
```json
{
  "param_name": {
    "min": 2.1, "mean": 4.3, "max": 7.8,
    "n": 10,
    "head": [2.1, 3.0],
    "tail": [5.2, 4.8],
    "trend": "rising"
  }
}
```
Trend: `late = mean(tail)`, `early = mean(head)`.
- rising: late > early × 1.3
- falling: late < early × 0.7
- stable: иначе

### Ансамбли
Каждый параметр агрегируется поверх members: p10 (10-й перцентиль), p50 (медиана), p90 (90-й), spread (p90 − p10).
```json
{
  "param_p10": [...], "param_p50": [...], "param_p90": [...], "param_spread": [...]
}
```

---

## 1. Open-Meteo — ОСНОВНОЙ ИСТОЧНИК

Все вызовы: `timezone=Europe/Berlin`, `windspeed_unit=ms`, `start_date=DATE`, `end_date=DATE`.

### 1.1 ECMWF Family (fallback chain)

```
Цепочка: ecmwf_ifs025 → ecmwf_ifs04
Endpoint: /v1/forecast?models=<model>

GET /v1/forecast?models=ecmwf_ifs025
  &hourly=temperature_2m,dewpoint_2m,relative_humidity_2m,
    windspeed_10m,windgusts_10m,winddirection_10m,
    cloudcover,cloudcover_low,cloudcover_mid,cloudcover_high,
    precipitation,cape,
    shortwave_radiation,direct_radiation,sunshine_duration,
    temperature_850hPa,temperature_700hPa,
    relative_humidity_850hPa,relative_humidity_700hPa,
    windspeed_850hPa,winddirection_850hPa,
    windspeed_700hPa,winddirection_700hPa

Если ecmwf_ifs025 не дал данных → тот же запрос с models=ecmwf_ifs04
```

### 1.2 ICON Family (fallback chain)

```
Цепочка: icon_d2 → icon_eu → icon_global
Endpoint: /v1/dwd-icon?models=<model>

Тот же набор параметров что и ECMWF + `updraft` (м/с).
`updraft` — максимальная вертикальная скорость конвективного восходящего потока (от поверхности до 10 км).
Фактически `updraft` возвращает данные только из ICON D2 (2 км, ≤48ч); EU/Global возвращают null.
icon_d2 доступен только ≤48ч; для дальних дат → icon_eu или icon_global.
```

### 1.3 GFS (единственная с BL height, LI, CIN)

Тот же набор + `boundary_layer_height, convective_inhibition, lifted_index, temperature_500hPa`.
Endpoint `/v1/gfs`, `models=gfs_seamless`.

Примечание: ECMWF и ICON принимают `convective_inhibition` как параметр, но возвращают null — фактически только GFS.

### 1.4 Ансамбли (ECMWF ENS + ICON-EU EPS)

```
GET https://ensemble-api.open-meteo.com/v1/ensemble
  ?models=ecmwf_ifs025  (или icon_eu)
  &hourly=temperature_2m,windspeed_10m,windgusts_10m,
    cloudcover,precipitation,cape,windspeed_850hPa
```
Ответ: `{param}_member00`, …, `{param}_member50`. Агрегация → p10/p50/p90/spread.

### 1.5 Извлекаемые переменные

| Параметр | Ключ | Модели | Назначение |
|----------|------|--------|------------|
| T 2m, Td 2m | temperature_2m, dewpoint_2m | ICON*, ECMWF*, GFS | LCL / база облаков |
| RH 2m / 850 / 700 | relative_humidity_* | ICON*, ECMWF*, GFS | Влажность |
| Ветер 10m + порывы | windspeed_10m, windgusts_10m | ICON*, ECMWF*, GFS | Приземный ветер |
| Направление 10m | winddirection_10m | ICON*, ECMWF*, GFS | Анализ ветра |
| Ветер 850/700 hPa | windspeed_850/700hPa | ICON*, ECMWF*, GFS | Фоновый ветер |
| Направление 850/700 | winddirection_850/700hPa | ICON*, ECMWF*, GFS | Анализ ветра |
| T 850/700/500 hPa | temperature_*hPa | ICON*, ECMWF*, GFS (500 только GFS) | Lapse rate |
| Облачность total/low/mid/high | cloudcover_* | ICON*, ECMWF*, GFS | Прогрев + инверсии |
| BL height | boundary_layer_height | **GFS only** | Глубина перемешивания → W* |
| CAPE | cape | ICON*, ECMWF*, GFS + ens | Конвективная энергия |
| Updraft | updraft | **ICON D2 only** (≤48ч) | Нативная конвективная скорость (м/с) |
| CIN | convective_inhibition | **GFS only** | Конвективное торможение (фактически) |
| LI | lifted_index | **GFS only** | Индекс нестабильности |
| SW / direct radiation | shortwave_radiation, direct_radiation | ICON*, ECMWF*, GFS | Прогрев → W* |
| Sunshine duration | sunshine_duration | ICON*, ECMWF*, GFS | Длительность солнца |
| Осадки | precipitation | ICON*, ECMWF*, GFS + ens | Осадки |

\* ICON = icon_d2 / icon_eu / icon_global (одна из chain); ECMWF = ecmwf_ifs025 / ecmwf_ifs04 (одна из chain)

### 1.6 Расчёты

**Cloud base MSL:** `125 × (T_2m − Td_2m) + elevation`
**Lapse rate:** `(T_850 − T_700) / 1.5` °C/km (>7 сильная, >8 очень сильная)
**W* (Deardorff):** `(g/T_K × BL_h × H_s / (ρ·cp))^(1/3)` где `H_s = 0.4 × SWR`, `ρ = 1.1`, `cp = 1005`
  - < 0.5 слабо, 1.0–1.5 умеренно, 1.5–2.5 хорошо, 2.5+ сильно
  - Требует GFS (BL height) — без GFS W* не считается
**Gust factor:** `windgusts_10m − windspeed_10m` (>7 → турбулентность)

---

## 2. GeoSphere AROME 2.5 km

```
GET https://dataset.api.hub.geosphere.at/v1/timeseries/forecast/nwp-v1-1h-2500m
  ?parameters=t2m,cape,cin,tcc,lcc,mcc,hcc,u10m,v10m,ugust,vgust,snowlmt,rr,grad
  &lat_lon={lat},{lon}&output_format=geojson
```

Timestamps в **UTC** → конвертируются в local через `zoneinfo`.

**Маппинг имён → стандартные:**
| GeoSphere | Стандартное | Примечание |
|-----------|-------------|------------|
| t2m | temperature_2m | |
| cape | cape | |
| cin | convective_inhibition | |
| tcc | cloudcover | |
| lcc / mcc / hcc | cloudcover_low / _mid / _high | |
| grad | shortwave_radiation | |
| rr | precipitation | |
| snowlmt | snow_line | Уникальный параметр |
| u10m, v10m | → windspeed_10m, winddirection_10m | Вычисляется из u/v |
| ugust, vgust | → windgusts_10m | Вычисляется из u/v |

---

## 3. DWD MOSMIX — sanity check

KMZ с XML внутри. Timestamps в UTC → скрипт конвертирует в **Europe/Berlin** с учётом DST.

**Извлекаемые параметры:**
TTT (→ −273.15 °C), Td (→ −273.15 °C), FF, FX1, DD,
N, Neff, Nh, Nm, Nl, PPPP (→ ÷100 hPa), SunD1, Rad1h, RR1c, wwP, R101.

Данные хранятся как `hourly_local[param][hour_str]`. Извлекается `at_13_local`.
Доступен только для локаций с `mosmix_id` (Lenggries/Wallberg → 10963, Innsbruck → 11120).

---

## 4. Headless-скрапинг (Deno + Playwright, только Docker)

`scraper.ts` запускается как subprocess из `fetch_weather.py`. Timeout 180s.

### 4.1 Meteo-Parapente
Загружает `https://meteo-parapente.com/#/{lat},{lon},11`, кликает по карте, перехватывает JSON-ответы API и парсит DOM (windgram, sounding, панели прогноза).

### 4.2 XContest
Загружает поиск полётов рядом с точкой (`filter[point]`, `filter[radius]=50000m`). Извлекает таблицу: пилот, старт, дистанция, тип. Работает как sanity check — летали ли люди.

### 4.3 ALPTHERM
Загружает `https://flugwetter.austrocontrol.at/`. Если нет login wall — извлекает таблицы и ссылки на карты термиков. Не привязан к конкретной локации (австрийский обзор).

---

## 5. Фён — данные есть, логика не реализована

Сырые данные для определения фёна доступны в hourly profile:
- `windspeed_700hPa`, `winddirection_700hPa` → южный ветер > 10 м/с
- `relative_humidity_700hPa` → < 30%
- `lapse_rate` → аномально высокий
- `snow_line` (GeoSphere) → аномальный рост

**Автоматическая детекция фёна НЕ реализована** в текущем коде.

---

## 6. Ключевые координаты

| Район | Lat | Lon | Elev | Peaks | MOSMIX | GeoSphere ID | Drive (h) |
|-------|-----|-----|------|-------|--------|---------------|-----------|
| Lenggries | 47.68 | 11.57 | 700 | 1800 | 10963 | — | 1.0 |
| Wallberg | 47.64 | 11.79 | 1620 | 1722 | 10963 | — | 1.0 |
| Kössen | 47.67 | 12.40 | 590 | 1900 | — | 11130 | 1.5 |
| Innsbruck | 47.26 | 11.39 | 578 | 2600 | 11120 | 11121 | 2.0 |
| Greifenburg | 46.75 | 13.18 | 600 | 2800 | — | 11204 | 4.0 |
| Speikboden | 46.90 | 11.87 | 950 | 2500 | — | — | 3.5 |
| Bassano | 45.78 | 11.73 | 130 | 1700 | — | — | 5.0 |

München (48.14, 11.58, 520m, MOSMIX 10865) — использовался ранее, **в текущем LOCATIONS отсутствует**.
