# Требования: Метео-триаж для XC closed routes

**Версия:** 3.3

---

## 1. Цель

Автоматизированный отбор «больших дней» для замкнутых маршрутов на параплане (closed route / return-to-area). Не пропускать 200+ км closed. Не тратить время на слабые дни.

| Приоритет | Дистанция | Тип |
|-----------|-----------|-----|
| 1 | 200+ км | closed route |
| 2 | 150+ км | closed route |
| 3 | 100+ км | closed route |

FAI-треугольник не обязателен. Open distance не интересен.

---

## 2. Пилот и логистика

| Параметр | Значение |
|----------|----------|
| База | Мюнхен |
| Макс. подъезд | 8 ч на машине |
| Крыло | Спортивный класс |
| Навыки | Очень высокие |
| Retrieve | Соло |
| Ранний выезд | OK |

---

## 3. Регионы

| Приоритет | Район | Якорные старты | Категория |
|-----------|-------|----------------|-----------|
| 1 | Баварские Альпы | Lenggries, Wallberg | Ближний |
| 2 | Kössen | Kössen | Ближний |
| 3 | Тироль | По ситуации | Средний |
| 4 | Южный Тироль | Greifenburg, Speikboden | Средний |
| 5 | Северная Италия | Bassano | Дальний |
| 6 | Швейцария | По ситуации | Только 150+/200+ |
| 7 | Словения | По ситуации | Только 150+/200+ |

**Исключено:** Чехия.

---

## 4. Модельный стек

### D-5…D-1 (среднесрок)
| Модель | Разрешение | Поставщик | Роль |
|--------|------------|-----------|------|
| ECMWF IFS HRES | 0.25° | Open-Meteo `/v1/forecast` | Опорная / скелет |
| ICON Seamless | blend | Open-Meteo `/v1/forecast` | Второй детерминист |
| GFS | ~0.25° | Open-Meteo `/v1/gfs` | BL height, LI, CIN (эксклюзивно) |
| ECMWF ENS (51 чл.) | 0.25° | `ensemble-api.open-meteo.com` | Ансамблевый разброс |
| ICON-EU EPS (40 чл.) | ~7 km | `ensemble-api.open-meteo.com` | Ансамблевый разброс |

### D-2…D-0 (ближний)
| Модель | Разрешение | Поставщик | Роль |
|--------|------------|-----------|------|
| ICON-D2 | 2 km | Open-Meteo `/v1/dwd-icon` | Локальный оверрайд 0–48 ч |
| GeoSphere AROME | 2.5 km | `dataset.api.hub.geosphere.at` | Хай-рез, полный ряд (UTC) |
| DWD MOSMIX | точечный | `opendata.dwd.de` | Sanity-check (только с mosmix_id) |

### Headless (только Docker)
| Источник | Скрапер | Роль |
|----------|---------|------|
| Meteo-Parapente | Playwright | Windgram / sounding |
| XContest | Playwright | Реальные полёты |
| ALPTHERM | Playwright | Термики (Austro Control) |

**BrightSky forecast — убран.** TAWES observations и автодетекция фёна — **не реализованы**.

---

## 5. Погодные пороги (привязаны к окну)

Все пороги оцениваются **по термическому окну** (09:00–18:00 Europe/Berlin), а не по одной точке.

### Флаги-стопперы (CRITICAL — вес −3 в scoring)

| Параметр | Порог | Агрегация | Флаг |
|----------|-------|-----------|------|
| Средний ветер 850 hPa | > 5 м/с | mean по окну | SUSTAINED_WIND_850 |
| Порывы 10 м | > 10 м/с | max по окну | GUSTS_HIGH |
| Осадки @13:00 | > 0.5 мм/ч | at_13_local | PRECIP_13 |
| Безвылетное окно | 0 ч | compute_flyable | NO_FLYABLE_WINDOW |

### Флаги качества (QUALITY — вес −1)

| Параметр | Порог | Агрегация | Флаг |
|----------|-------|-----------|------|
| Облачность @13:00 | > 80% | at_13_local | OVERCAST |
| Lapse rate | < 5.5 °C/km | mean по окну | STABLE |
| Термическое окно | < 5 ч | thermal_window_hours | SHORT_WINDOW |
| Gust factor (порыв − средний) | > 7 м/с | max по окну | GUST_FACTOR |

### Облачная база (LOW_BASE — вес −2)

| Параметр | Порог | Агрегация | Флаг |
|----------|-------|-----------|------|
| CB min по окну | < вершины + 1000 м | min по окну | LOW_BASE |

### Флаги опасности (DANGER — вес −1)

| Параметр | Порог | Агрегация | Флаг |
|----------|-------|-----------|------|
| CAPE max | > 1500 J/kg | max по окну | HIGH_CAPE |
| CAPE тренд | late > early × 1.5 и late > 800 | head vs tail | CAPE_RISING |
| LI @13:00 | < −4 | at_13_local | VERY_UNSTABLE |

### Позитивные индикаторы (v2.2)

| Параметр | Порог | Агрегация | Флаг | Вес |
|----------|-------|-----------|------|-----|
| Lapse rate max | > 7 °C/km | max по окну | STRONG_LAPSE | +1 |
| CAPE peak | 300–1500 J/kg | max по окну | GOOD_CAPE | +1 |
| BL height max | > 1500 м | max по окну | DEEP_BL | +1 |
| CB max | > peaks + 1500 м | max по окну | HIGH_BASE | +1 |
| CB max | > 3500 м MSL | max по окну | VERY_HIGH_BASE | +2 |
| Термическое окно | ≥ 7 ч | thermal_window_hours | LONG_WINDOW | +1 |
| Облачность @13:00 | < 30% | at_13_local | CLEAR_SKY | +1 |
| W* max | ≥ 1.5 м/с | max по окну | GOOD_WSTAR | +1 |
| SW radiation max | > 600 W/m² | max по окну | STRONG_SUN | +1 |

> HIGH_BASE и VERY_HIGH_BASE взаимоисключающие: если база > 3500 → VERY_HIGH_BASE (+2), иначе если > peaks+1500 → HIGH_BASE (+1).

### Модельные флаги (динамические)

| Условие | Действие | Флаг |
|---------|----------|------|
| Model agreement < 50% + GO/STRONG | → MAYBE | LOW_CONFIDENCE |
| Ensemble wind spread > 5 м/с + GO/STRONG | → MAYBE | ENS_WIND_SPREAD |
| Ensemble CAPE spread > 1000 J/kg + GO/STRONG | → MAYBE | ENS_CAPE_SPREAD |

### Определение «рабочее окно»
Непрерывный интервал (от 1 ч и более), где одновременно:
- нет организованных осадков (precip ≤ 0.5 mm)
- порывы ≤ 12 м/с
- ветер 10 м ≤ 8 м/с

`continuous_flyable_hours` = длительность самого длинного такого непрерывного отрезка.

---

## 6. Привязка ко времени

### Часовой пояс
Все ключевые оценки — **13:00 local (Europe/Berlin)** с учётом DST.
- CET (зима): 13:00 local = 12:00 UTC
- CEST (лето): 13:00 local = 11:00 UTC

Конвертация через `zoneinfo.ZoneInfo("Europe/Berlin")`, не через захардкоженный оффсет.

### Слои данных (для каждого источника)

**at_13_local** — значения точно в 13:00 local:
| Параметр | Единицы |
|----------|---------|
| temperature_2m | °C |
| dewpoint_2m | °C |
| relative_humidity_2m | % |
| windspeed_10m / windgusts_10m / winddirection_10m | m/s, ° |
| cloudcover (+ low/mid/high) | % |
| precipitation | mm |
| cape | J/kg |
| shortwave_radiation | W/m² |
| temperature_850hPa / temperature_700hPa | °C |
| relative_humidity_850hPa / 700hPa | % |
| windspeed_850hPa / 700hPa | m/s |

**thermal_window_stats** — агрегаты по окну 09:00–18:00 local:
| Статистика | Описание |
|------------|----------|
| min | минимум по окну |
| mean | среднее по окну |
| max | максимум по окну |
| n | кол-во точек |
| head | первые 1–2 значения (09:00–10:00) |
| tail | последние 1–2 значения (17:00–18:00) |
| trend | rising / falling / stable (если ≥ 4 точек) |

Тренд: `late = mean(tail)`, `early = mean(head)`.
- rising: late > early × 1.3
- falling: late < early × 0.7
- stable: иначе

### Ансамблевые агрегаты (для ECMWF ENS, ICON-EU EPS)
Для каждого параметра хранить: **p10, p50, p90, spread** (p90 − p10).

---

## 7. XC-профиль

По умолчанию: **термичный**. Ridge/dynamic — только по запросу.

---

## 8. Шкала оценки (реализованная)

Автоматический статус для каждой локации (не по дистанции):

| Статус | Значение | Score range |
|--------|----------|-------------|
| NO-GO | Нереалистично | ≤ −5 или ≥2 критических |
| UNLIKELY | Маловероятно | ≤ −2 |
| MAYBE | При удачном стечении | ≤ 1 |
| GO | Уверенный шанс | ≤ 4 |
| STRONG | Big day | > 4 |
| NO DATA | Нет данных | 0 флагов и 0 позитивов |

---

## 9. Pipeline анализа (реализованный)

### Этап 1 — Сбор данных (`assess_location`)
Для каждой локации последовательно (каждый источник независим от остальных):
1. Deterministic: ECMWF HRES → ICON Seamless → ICON-D2 → GFS
2. Ensemble: ECMWF ENS → ICON-EU EPS
3. GeoSphere AROME (если координаты в зоне покрытия)
4. DWD MOSMIX (если есть `mosmix_id`)
5. Headless scrapers (Docker, после всех API)

Для каждого источника извлекаются `at_13_local` и `thermal_window_stats`.

### Этап 2 — Hourly Profile (`build_hourly_profile`)
Combined profile по часам 08:00–18:00 из best available:
- **Приоритет surface/pressure:** ICON-D2 > ECMWF HRES > ICON Seamless > GFS
- **GFS-only параметры:** boundary_layer_height, lifted_index, convective_inhibition
- **Updraft (м/с):** нативный параметр ICON D2 (2 км, ≤48ч) — макс. вертикальная скорость конвективного восходящего потока. EU/Global возвращают null
- **Fallback SW/CAPE:** сначала приоритетная цепочка, затем GFS

→ Для каждого часа и поля фиксируется `_src` (доминирующий источник) и `_src_overrides` (исключения).

### Этап 3 — Thermal Window Detection
Часы, где одновременно: W* ≥ 1.5, precip ≤ 0.5, base ≥ 1000m MSL, cloud < 60%.
→ `thermal_window`: start, end, duration_h, peak_hour (макс lapse + cape).

### Этап 4 — Flyable Window (`compute_flyable_window`)
Самый длинный непрерывный отрезок (09:00–18:00) где одновременно:
- precip ≤ 0.5 мм
- порывы ≤ 12 м/с
- ветер 10м ≤ 8 м/с

→ `continuous_flyable_hours`, `flyable_start`, `flyable_end`.

### Этап 5 — Flags & Positives (`compute_flags`)
Анализ hourly profile по пороговым значениям → списки flags и positives.

### Этап 6 — Model Agreement (`compute_model_agreement`)
Автоматическое сравнение ECMWF HRES vs ICON Seamless at_13_local.
- Параметры: temperature_2m, windspeed_10m, windgusts_10m, cloudcover, precipitation, cape, windspeed_850hPa
- Допуски: T ±2°C, wind ±2 m/s, gusts ±3 m/s, cloud ±20%, precip ±0.5 mm, CAPE ±200 J/kg
- Score → confidence: HIGH (≥80%) / MEDIUM (≥50%) / LOW (<50%)

### Этап 7 — Ensemble Uncertainty (`compute_ensemble_uncertainty`)
Spreads ECMWF ENS + ICON-EU EPS at_13_local.
Большой spread → понижение статуса.

### Этап 8 — Scoring & Status (`compute_status`)
Формула: score = −3×critical − 2×low_base − 1×quality − 1×danger + Σ(positive weights).
Веса позитивных: VERY_HIGH_BASE = +2, остальные = +1.
Жёсткие правила: критические флаги и модельная неуверенность могут понизить статус.

### Этап 9 — Финальный ранжир
Все локации сортируются по STATUS_ORDER: STRONG > GO > MAYBE > UNLIKELY > NO-GO > NO DATA.

---

## 10. Режимы запуска

| Режим | Когда | Фокус |
|-------|-------|-------|
| Предварительный | T−2..3 дня | Отбор кандидатов, разброс ансамблей |
| Подтверждение | Вечер T−1 | Свежие модели, тренд, подтверждение |
| Утренний | Утро T | Только по запросу |

---

## 11. Формат выдачи

1. **Краткий итог** (2–6 строк): лучший вариант, оценки, база, уверенность
2. **Таблица** (3–6 строк): район, статус, оценки, окно, база, ветер, потоки, риски, согласованность, confidence
3. **Лучший вариант**: тайминг, профиль, критичные участки, что сломает день
4. **Остальные**: кратко, при каком сдвиге станут лучшими
5. **Сомнения / Неуверенности**: модельные расхождения, ансамблевый разброс, нехватка данных

Блок «Сомнения» выдаётся **всегда**, даже если расхождений нет (тогда: "Значительных расхождений не обнаружено").

---

## 12. Правила качества

- «Летабельно» ≠ «подходит для big closed day»
- Данные отделять от выводов
- Числа и диапазоны вместо расплывчатых формулировок
- Недостаток данных — указывать явно
- Честный MAYBE лучше ложной уверенности
- База всегда в MSL @13:00 local
- Все метрики привязаны к окну (не к одной точке), кроме at_13_local
- Для каждого поля @13 фиксируется, какая модель предоставила данные (`_sources`)

---

## 13. Scoring (реализованный алгоритм)

### Веса флагов

| Категория | Флаги | Вес |
|-----------|-------|-----|
| CRITICAL | SUSTAINED_WIND_850, GUSTS_HIGH, PRECIP_13, NO_FLYABLE_WINDOW | −3 |
| LOW_BASE | LOW_BASE | −2 |
| QUALITY | OVERCAST, STABLE, SHORT_WINDOW, GUST_FACTOR | −1 |
| DANGER | HIGH_CAPE, VERY_UNSTABLE, CAPE_RISING | −1 |
| POSITIVE | STRONG_LAPSE, GOOD_CAPE, DEEP_BL, HIGH_BASE, LONG_WINDOW, CLEAR_SKY, GOOD_WSTAR, STRONG_SUN | +1 |
| VERY_HIGH_BASE | VERY_HIGH_BASE (взамен HIGH_BASE, если база > 3500 м MSL) | +2 |

### Формула
```
score = −3 × n_critical − 2 × n_low_base − 1 × n_quality − 1 × n_danger + 1 × n_positive + 2 × n_very_high_base
```

### Пороги статуса

| Score | Статус |
|-------|--------|
| ≤ −5 | NO-GO |
| ≤ −2 | UNLIKELY |
| ≤ 1 | MAYBE |
| ≤ 4 | GO |
| > 4 | STRONG |

### Жёсткие правила понижения

1. ≥ 2 критических **ИЛИ** (≥ 1 критический + LOW_BASE) → **NO-GO** (безусловно)
2. ≥ 1 критический + статус GO/STRONG → **MAYBE**
3. Model agreement confidence = LOW + статус GO/STRONG → **MAYBE** + `LOW_CONFIDENCE`
4. Ensemble wind spread > 5 м/с + GO/STRONG → **MAYBE** + `ENS_WIND_SPREAD`
5. Ensemble CAPE spread > 1000 J/kg + GO/STRONG → **MAYBE** + `ENS_CAPE_SPREAD`
6. 0 критических + 0 quality + 0 positives → **NO DATA**
