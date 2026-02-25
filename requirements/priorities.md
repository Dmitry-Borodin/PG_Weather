# Приоритеты получения и использования данных

**Версия:** 2.1

---

## 1. Концепция: Family-based Fallback Chains

В v2.0 детерминистические модели организованы в **семейства** (families).
Внутри каждого семейства модели упорядочены от **наиболее детальной к наиболее грубой**.
Скрипт пробует модели в цепочке по порядку; как только получена валидная модель —
более грубые варианты того же семейства **не запрашиваются**.

Преимущества:
- Меньше API-запросов (не дублируем ICON-D2 + ICON-EU + ICON Global)
- Явно фиксируется, какая именно модель была получена (`_hourly_raw`, ключ в `sources`)
- При сбое детальной модели автоматический fallback к грубой

---

## 2. Fallback Chains (Fetch Pipeline)

### 2.1 ICON Family

```
GET /v1/dwd-icon

ICON-D2  2 km  (48h range)
  ↓ no data for target date or error
ICON-EU  7 km  (120h range)
  ↓ no data or error
ICON Global  13 km  (180h+ range)
```

- **API endpoint:** `api.open-meteo.com/v1/dwd-icon?models=<model>`
- **Параметры:** температура, влажность, ветер (surface + 850/700 hPa), облачность, осадки, CAPE, SW radiation, sunshine duration
- **Роль:** высокоразрешённый детерминист для Альп
- ICON-D2 доступен только на ≤48ч вперёд; ≥3 дней → EU или Global
- Результат сохраняется под ключом успешной модели: `icon_d2`, `icon_eu` или `icon_global`

### 2.2 ECMWF Family

```
GET /v1/forecast

ECMWF IFS 0.25°  (ecmwf_ifs025)
  ↓ no data or error
ECMWF IFS 0.4°  (ecmwf_ifs04)
```

- **API endpoint:** `api.open-meteo.com/v1/forecast?models=<model>`
- **Параметры:** аналогичные ICON (без BL height / LI / CIN)
- **Роль:** опорная глобальная модель, скелет прогноза
- Результат: `ecmwf_ifs025` или `ecmwf_ifs04`

### 2.3 GFS Family

```
GET /v1/gfs

GFS Seamless  (auto-blend)
```

- **API endpoint:** `api.open-meteo.com/v1/gfs?models=gfs_seamless`
- **Параметры:** все из ECMWF + `boundary_layer_height`, `lifted_index`, `convective_inhibition`, `temperature_500hPa`
- **Роль:** ЕДИНСТВЕННЫЙ источник BL height, LI, CIN → необходим для W*
- Цепочка из одной модели (seamless blend)
- Потеря GFS = нет W*, нет thermal window detection

### 2.4 Ансамблевые модели

```
ECMWF ENS (51 member)   → ensemble-api.open-meteo.com?models=ecmwf_ifs025
ICON-EU EPS (40 member)  → ensemble-api.open-meteo.com?models=icon_eu
```

- Каждый ансамбль скачивается **независимо**, без fallback chain
- Агрегируются в `p10 / p50 / p90 / spread` per-timestep
- **Параметры ансамбля:** temperature_2m, windspeed_10m, windgusts_10m, cloudcover, precipitation, cape, windspeed_850hPa

### 2.5 Региональные источники

```
GeoSphere AROME 2.5 km
  → dataset.api.hub.geosphere.at (GeoJSON, UTC timestamps)
  → t2m, cape, cin, облачность, ветер u/v, порывы u/v, snowlmt, осадки, SW radiation
  → Роль: хай-рез для Австрии + CIN + snow line

DWD MOSMIX_L (только если есть mosmix_id у локации)
  → KMZ → XML → parse
  → TTT, Td, FF, FX1, DD, N, Neff, Nh, Nm, Nl, PPPP, SunD1, Rad1h, RR1c, wwP, R101
  → Роль: точечный sanity-check, НЕ УЧАСТВУЕТ в hourly profile
```

### 2.6 Headless-скрапинг (только Docker)

```
Meteo-Parapente     → перехват XHR (thermal profiles, sounding data)
XContest            → реальные полёты (r=50km от точки)
ALPTHERM            → обзорная карта термиков Austro Control
```

- Запуск: `deno run -A scraper.ts`, subprocess, timeout 180s
- Метео-параплан обрабатывается `integrate_meteo_parapente()` — вычисляет
  max thermal, thermal top, PBLH и добавляет флаги `MP_STRONG_THERMALS` / `MP_WEAK_THERMALS`

---

## 3. Порядок скачивания (per location)

```
ДЛЯ КАЖДОЙ ЛОКАЦИИ (последовательно):

  1. ICON chain:   D2 → EU → Global   (первый успех → стоп)
  2. ECMWF chain:  IFS 0.25 → IFS 0.4  (первый успех → стоп)
  3. GFS chain:    Seamless             (один запрос)
  4. ECMWF ENS:    51-member           (агрегация)
  5. ICON-EU EPS:  40-member           (агрегация)
  6. GeoSphere AROME                   (если не упал)
  7. MOSMIX                            (если есть mosmix_id)

ПОСЛЕ ВСЕХ ЛОКАЦИЙ:
  8. Headless scraper (если не --no-scraper)
  9. integrate_meteo_parapente()       (post-processing)
```

Каждый источник обёрнут в try/except — ошибка одного не влияет на остальные.
При ошибке: `{"error": "..."}` записывается в `sources[key]`, pipeline продолжается.

---

## 4. Приоритет ИСПОЛЬЗОВАНИЯ данных (Averaged Hourly Profile, v2.1)

Для scoring строится **усреднённый профиль** (08:00–18:00), где общие параметры
— среднее арифметическое best ICON и best ECMWF.

### 4.1 Определение доступных слоёв (`_find_available_sources`)

Динамически определяется, какая именно модель из каждого семейства была получена:

```
ICON family:   icon_d2 | icon_eu | icon_global | icon_seamless(legacy)
ECMWF family:  ecmwf_ifs025 | ecmwf_ifs04 | ecmwf_hres(legacy)
GFS family:    gfs_seamless | gfs(legacy)
```

### 4.2 Усреднение (v2.1)

```
Общие параметры (temp, wind, cloud, precip, CAPE, SW, lapse...):
  avg(Best ICON, Best ECMWF)
  Если одна модель = None — берётся другая

GFS-only (нет fallback):
  • boundary_layer_height  → нужен для W*
  • lifted_index           → LI @13 для VERY_UNSTABLE
  • convective_inhibition  → CIN

ICON-only:
  • updraft                → ICON D2 native (EU/Global = null)

Расчётные поля (cloudbase, wstar, gust_factor):
  Пересчитываются на усреднённых входных значениях
```

Если GFS не скачался — все три = null, W* = null → thermal window пустое.

```
1. Попытка: цепочка ICON > ECMWF > GFS (обычный приоритет)
2. Если null по цепочке: отдельный запрос _pick_gfs
```

### 4.5 Что НЕ участвует в combined profile

| Источник | Роль в системе |
|----------|---------------|
| GeoSphere AROME | Отдельный блок `thermal_window_stats` в отчёте |
| DWD MOSMIX | Отдельный блок `hourly_local` в отчёте |
| Ансамбли (ENS, EPS) | `ensemble_uncertainty` — влияют на scoring/статус |
| Headless (MP, XC, ALT) | Сырые данные в `sources` + MP integration |

---

## 5. Per-Model Profiles (v2.0)

Помимо combined profile, для каждой **отдельной** детерминистической модели
строится **индивидуальный hourly profile** (`build_per_model_profiles`).

Это позволяет:
- Проверить каждую модель по отдельности
- Показать таблички hourly данных per-model в HTML-вьюере (tabbed interface)
- Вычислить per-model assessment (упрощённый GO / MAYBE / UNLIKELY / NO-GO)

### 5.1 Per-model Assessment (`assess_per_model`)

Для каждой модели строится отдельная оценка (упрощённая логика):

| Условие | Статус |
|---------|--------|
| precip @13 > 0.5 OR flyable_hours = 0 OR mean wind_850 > 5 | NO-GO |
| thermal_hours ≤ 2 OR flyable_hours < 4 | UNLIKELY |
| thermal_hours ≤ 4 | MAYBE |
| thermal_hours > 4 | GO |

Если хотя бы одна модель дала NO-GO/UNLIKELY — это учитывается как **MODEL_DISAGREE**
при финальной оценке (Hard Rule 4, см. flyability.md).

---

## 6. Model Agreement (v2.0)

Сравниваются **лучшие доступные** ECMWF и ICON модели at 13:00 local:

```
Best ECMWF: ecmwf_ifs025 (или ecmwf_ifs04 / ecmwf_hres)
Best ICON:  icon_d2 (или icon_eu / icon_global / icon_seamless)
```

Параметры и допуски для agreement без изменений (см. flyability.md §8).

---

## 7. Критичность потери источников (v2.0)

| Источник | Потеря | Влияние |
|----------|--------|---------|
| GFS | **Критичное** | Нет W*, нет LI, нет CIN → thermal window = 0 → base score = −6 |
| ECMWF (вся chain) | **Серьёзное** | Нет опорной модели, model agreement = UNKNOWN |
| ICON (вся chain) | **Серьёзное** | Нет model agreement, combined profile = только ECMWF + GFS |
| ICON-D2 (но EU дал) | Низкое | Fallback на EU 7km — лишь немного грубее |
| ECMWF ENS | Низкое | Нет ансамблевого spread для ECMWF |
| ICON-EU EPS | Низкое | Нет ансамблевого spread для ICON |
| GeoSphere | Низкое | Нет австрийского хай-реза, нет snow line |
| MOSMIX | Минимальное | Нет sanity-check |
| Headless | Минимальное | Нет MP/XC/ALPTHERM (дополнительные данные) |

---

## 8. Сводка data flow (v2.0)

```
      FETCH (per location, каждый источник независим)
      ─────────────────────────────────────────────
      ICON chain          → icon_d2 | icon_eu | icon_global
      ECMWF chain         → ecmwf_ifs025 | ecmwf_ifs04
      GFS (always needed) → gfs_seamless
      ECMWF ENS           → p10/p50/p90/spread
      ICON-EU EPS         → p10/p50/p90/spread
      GeoSphere           → отдельный блок
      MOSMIX              → отдельный блок
                                  │
                                  ▼
      AVERAGING (v2.1: усреднённый профиль для scoring)
      ────────────────────────────────────────────
      avg(Best ICON, Best ECMWF) → общие параметры
                                        │
                              GFS-only: BL, LI, CIN
                              ICON-only: updraft
                                  │
                                  ▼
      PER-MODEL PROFILES (each model → separate table)
                                  │
                                  ▼
      ANALYSIS
      ────────
      Combined:   Thermal window → Flyable window → Flags → Positives (на усреднённом профиле)
      Per-model:  assess_per_model → MODEL_DISAGREE check
      Agreement:  Best ECMWF vs Best ICON @13:00
      Ensemble:   spread → ENS_WIND_SPREAD / ENS_CAPE_SPREAD checks
      MP:         integrate_meteo_parapente → MP_STRONG / MP_WEAK
                                  │
                                  ▼
      SCORING
      ───────
      base_score (tw_hours) → deductions → bonuses → hard rules → status
                                  │
                                  ▼
      REPORT
      ──────
      JSON + Markdown + HTML viewer (ICON/ECMWF/GFS tables + per-model tabs + ensemble + GeoSphere)
```
