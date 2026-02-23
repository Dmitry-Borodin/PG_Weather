# Источники данных для метео-триажа

**Версия:** 1.0

---

## Сводка протестированных источников

| # | Источник | Метод | Статус | Данные |
|---|----------|-------|--------|--------|
| 1 | **Open-Meteo** (ICON-D2/GFS/ECMWF) | curl → JSON | ✅ Работает | T, Td, ветер 10m/850/700hPa, порывы, облачность, осадки, CAPE, CIN, LI, BL height |
| 2 | **GeoSphere Austria** (TAWES станции) | curl → JSON | ✅ Работает | Текущие T, ветер, порывы, направление по станциям AT |
| 3 | **GeoSphere NWP** (nwp-v1-1h-2500m) | curl → JSON | ✅ Работает | Прогноз T2m, CAPE, CIN, облачность, ветер u/v, порывы, снеговая линия |
| 4 | **DWD MOSMIX** | curl KMZ → XML | ✅ Работает | 114 параметров: T, Td, ветер, порывы, облачность по уровням, осадки, видимость, солн. радиация |
| 5 | **BrightSky** (DWD wrapper) | curl → JSON | ✅ Работает | T, Td, ветер, порывы, облачность, осадки, давление, видимость, солнце |
| 6 | **RainViewer** | curl → JSON | ✅ Работает | Радарные тайлы (осадки/nowcast) |
| 7 | **thermal.kk7.ch** | curl → KML/GeoJSON | ⚠️ Частично | Термические хотспоты (из IGC/OGN треков), не прогноз |
| 8 | **Meteo-Parapente** | headless браузер | ⚠️ Нужен headless | Термики м/с, база, ветер (hi-res). Нет публичного API |
| 9 | **ALPTHERM** (Austro Control) | headless / login | ⚠️ За логином | Thermikqualität, Steigwerte для Австрии |
| 10 | **RASP / BLIPMAP** | curl → PNG/text | ⚠️ Сервер нестабилен | W*, HBL, B/S, soaring params (WRF-based) |
| 11 | **DWD FlugWetter** | login required | ❌ Закрыт | PFD, thermal top. Нужна регистрация как пилот |
| 12 | **SkySight** | canvas render | ❌ Не парсится | Thermal strength, convergence. Платный, canvas |
| 13 | **TopMeteo** | PNG tiles | ❌ Не парсится | Thermal maps. Серверные тайлы |
| 14 | **meteoblue** | login/API key | ⚠️ Нужен ключ | Soaring index, BL height, convective updraft |
| 15 | **burnair** | headless | ⚠️ Нужен headless | Thermikstärke, база, градиенты (Швейцария) |
| 16 | **XContest** | headless | ⚠️ HTML | Полёты (sanity check) |

---

## 1. Open-Meteo — ОСНОВНОЙ ИСТОЧНИК

Бесплатный REST JSON API, без ключа. Агрегирует множество моделей.

### 1.1 Мультимодельный запрос (ICON-D2 + GFS + ECMWF)

```bash
# ICON-D2 (2 км, Центральная Европа, до +48ч)
curl -s "https://api.open-meteo.com/v1/dwd-icon?latitude=47.68&longitude=11.57\
  &hourly=temperature_2m,dewpoint_2m,windspeed_10m,windgusts_10m,winddirection_10m,\
  cloudcover,cloudcover_low,cloudcover_mid,cloudcover_high,precipitation,cape,\
  temperature_850hPa,temperature_700hPa,windspeed_850hPa,winddirection_850hPa,\
  windspeed_700hPa,winddirection_700hPa\
  &models=icon_d2&timezone=Europe/Berlin&windspeed_unit=ms&forecast_days=2"

# GFS (глобальная, до +16 дней) — ЕДИНСТВЕННАЯ МОДЕЛЬ С BL HEIGHT
curl -s "https://api.open-meteo.com/v1/gfs?latitude=47.68&longitude=11.57\
  &hourly=temperature_2m,dewpoint_2m,windspeed_10m,windgusts_10m,\
  cape,convective_inhibition,lifted_index,boundary_layer_height,\
  cloudcover,precipitation,\
  temperature_850hPa,temperature_700hPa,temperature_500hPa,\
  windspeed_850hPa,windspeed_700hPa,winddirection_850hPa,winddirection_700hPa\
  &timezone=Europe/Berlin&windspeed_unit=ms&forecast_days=3"

# Мультимодельное сравнение (одним запросом)
curl -s "https://api.open-meteo.com/v1/forecast?latitude=47.68&longitude=11.57\
  &hourly=temperature_2m,dewpoint_2m,windspeed_10m,windgusts_10m,cape,cloudcover,precipitation\
  &models=icon_seamless,gfs_seamless,ecmwf_ifs025\
  &timezone=Europe/Berlin&windspeed_unit=ms&forecast_days=2"
# Ключи в ответе: temperature_2m_icon_seamless, temperature_2m_gfs_seamless, ...
```

### 1.2 Что извлекается

| Параметр | Open-Meteo поле | Модель | Проверено |
|----------|-----------------|--------|-----------|
| Ветер 850 гПа (~1500 м) | windspeed_850hPa | ICON-D2, GFS | ✅ |
| Ветер 700 гПа (~3000 м) | windspeed_700hPa | ICON-D2, GFS | ✅ |
| Порывы 10 м | windgusts_10m | ICON-D2, GFS | ✅ |
| Boundary layer height | boundary_layer_height | **Только GFS** | ✅ |
| CAPE | cape | ICON-D2, GFS, ECMWF | ✅ |
| CIN | convective_inhibition | GFS | ✅ |
| Lifted Index | lifted_index | GFS | ✅ |
| Облачность (total/low/mid/high) | cloudcover* | ICON-D2, GFS | ✅ |
| T 2m + Td 2m | temperature_2m, dewpoint_2m | Все | ✅ |
| T на уровнях давления | temperature_850hPa, _700hPa, _500hPa | ICON-D2, GFS | ✅ |
| Осадки | precipitation | Все | ✅ |

### 1.3 Расчёт базы облаков (LCL)

```
LCL_AGL ≈ 125 × (T_2m − Td_2m)   [метры]
LCL_MSL = LCL_AGL + elevation_станции
```

Open-Meteo даёт T_2m и Td_2m → можно вычислить.

### 1.4 Расчёт lapse rate

```
Γ = (T_850 − T_700) / 1.5   [°C/km]
```
- Γ > 7°C/km → сильная нестабильность
- Γ > 8°C/km → очень сильная нестабильность (DALR ≈ 9.8)

### 1.5 Оценка силы термиков (прокси)

Прямая сила (м/с) недоступна из NWP. Прокси:
1. **BL height** (GFS) → глубина перемешивания
2. **CAPE** → конвективная энергия (>300 J/kg → хорошо, >700 → сильно)
3. **Lapse rate** (см. выше)
4. **Lifted Index** → <0 = нестабильно, <-3 = сильно нестабильно
5. **CIN** → <-50 = конвективный блок (тяжело запуститься)

---

## 2. GeoSphere Austria — НАБЛЮДЕНИЯ + NWP

### 2.1 Текущие наблюдения станций (TAWES)

```bash
# Innsbruck, Kufstein, Lienz, Kitzbühel, Zell am See
curl -s "https://dataset.api.hub.geosphere.at/v1/station/current/tawes-v1-10min\
  ?parameters=TL,FF,FFX,DD,RF\
  &station_ids=11320,11121,11130,11204,11279,11144"
```

Ответ: GeoJSON с текущими T, ветер, порывы, направление, влажность.

**Ключевые станции:**
| Станция | ID | Высота | Район |
|---------|----|--------|-------|
| Innsbruck Uni | 11320 | 578 м | Inn valley |
| Innsbruck Airport | 11121 | 578 м | Фён-индикатор |
| Kufstein | 11130 | 490 м | Kössen area |
| Lienz | 11204 | 661 м | Greifenburg area |
| Kitzbühel | 11279 | 772 м | Тироль |
| Zell am See | 11144 | 754 м | Зальцбург/Пинцгау |
| St. Johann im Pongau | 11364 | 634 м | Зальцбург |

### 2.2 NWP Прогноз (AROME-based, 2.5 км)

```bash
curl -s "https://dataset.api.hub.geosphere.at/v1/timeseries/forecast/nwp-v1-1h-2500m\
  ?parameters=t2m,cape,cin,tcc,u10m,v10m,ugust,vgust,snowlmt\
  &lat_lon=47.26,11.39&output_format=geojson"
```

Параметры: t2m, cape, cin, облачность, ветер u/v, порывы u/v, снеговая линия, радиация, осадки.

**Snowlmt (снеговая линия)** — уникальный параметр: индикатор высоты нулевой изотермы. Полезен как прокси для оценки термической структуры.

---

## 3. DWD MOSMIX — ПРОГНОЗ ПО СТАНЦИЯМ

```bash
# Скачать KMZ (ZIP с KML) для станции Munich-Flughafen (10865)
curl -s "https://opendata.dwd.de/weather/local_forecasts/mos/MOSMIX_L/single_stations/10865/kml/MOSMIX_L_LATEST_10865.kmz" -o mosmix.kmz
# Внутри: XML/KML с 114 параметрами на ~240 часов вперёд
```

**Ключевые станции (DWD ID):**
| Станция | ID | Для чего |
|---------|----|----------|
| München-Flughafen | 10865 | Базовая точка |
| Garmisch-Partenkirchen | 10963 | PreAlps |
| Zugspitze | 10961 | High alpine reference |

**Полезные параметры MOSMIX:**
- TTT: температура 2m
- Td: точка росы
- FF: ветер
- FX1/FX3: порывы (1ч/3ч)
- N/Neff/Nh/Nm/Nl: облачность (total/effective/high/mid/low)
- PPPP: давление
- SunD1: часы солнца
- Rad1h: радиация (прокси для инсоляции → прогрев)
- RR1c: осадки

Парсинг: ZIP → KML → XML ElementTree.

---

## 4. BrightSky — УДОБНАЯ ОБЁРТКА DWD

```bash
curl -s "https://api.brightsky.dev/weather?lat=47.68&lon=11.57&date=2026-02-23"
```

Простой JSON с часовыми данными: T, Td, ветер, порывы, облачность, осадки, давление, видимость, sunshine.

Плюс: проще парсить, чем MOSMIX KMZ.
Минус: нет pressure-level данных, нет CAPE.

---

## 5. RainViewer — РАДАР/NOWCAST

```bash
curl -s "https://api.rainviewer.com/public/weather-maps.json"
```

Даёт список радарных тайлов (PNG). Можно проверить текущие/nowcast осадки по тайлу.

---

## 6. Соаринговые источники (из заметок)

### 6.1 RASP / BLIPMAP
- Даёт W* (thermal velocity), HBL, B/S ratio, Hcrit
- WRF-based, 3-4 км разрешение
- Серверы нестабильны. Может работать: `rasp-europe.org`
- **Метод:** curl → PNG карт + text файлы прогнозов

### 6.2 Meteo-Parapente
- Силу термиков (м/с), базу, ветер по высотам — hi-res
- Нет публичного API; данные приходят через XHR при загрузке карты
- **Метод:** headless-браузер (Playwright) → перехват XHR → JSON
- Нужна подписка для полного доступа

### 6.3 meteoblue
- Soaring Index, BL height, Convective Updraft, Soaring Flight Distance
- API с ключом (бесплатный уровень ограничен)
- **Метод:** headless или API-ключ

### 6.4 XC Therm
- Thermal forecasts по регионам Европы (hi-res Альпы)
- Подписка, нет публичного API

### 6.5 burnair
- Thermikstärke, база, градиенты для швейцарских/австрийских стартов
- **Метод:** headless

### 6.6 ALPTHERM (Austro Control)
- Thermikqualität, Steigwerte — классический продукт для Альп
- За логином на austrocontrol.at/flugwetter
- **Метод:** headless с авторизацией

### 6.7 DWD FlugWetter (PFD / thermal top)
- PFD (Predicted Flight Distance), Lifting ratio, Top of dry convection
- Обновление 4x/сутки, +78ч
- Требует регистрации как пилот/авиатор
- **Метод:** headless с авторизацией

### 6.8 thermalmap.info (thermal.kk7.ch)
- Карта термиков/синков из реальных IGC/OGN треков
- **Не прогноз**, а статистика: где обычно работает
- API: `/api/hotspots/`, `/api/hotspots/geojson/` — работает частично
- **Метод:** headless для карты, или GeoJSON API

---

## 7. Фён — специфические источники

Фён — критический фактор для Альп. Источники:

### 7.1 Фён-индикаторы из наблюдений
- **GeoSphere Innsbruck Airport (11121)**: классический фён-индикатор
  - Резкий рост T + падение влажности + сильные порывы с юга = фён
  - Мониторить: DD (направление 150-210°), FFX (порывы), RF (влажность <30%), T скачок
- **Разница давлений Север-Юг**: Innsbruck vs Bozen/Brenner

### 7.2 Фён из NWP
- **Ветер 700 гПа** (Open-Meteo): сильный южный поток на 700 гПа → фён
- **Lapse rate** резко отличается от нормы → адвекция тёплого воздуха сверху
- **GeoSphere snowlmt**: резкий рост снеговой линии = фён

### 7.3 Правила определения фёна из данных
```
FOEHN_LIKELY если:
  wind_direction_700hPa ∈ [150°, 230°]  # южный поток
  AND windspeed_700hPa > 10 м/с
  AND (T_innsbruck - T_ожидаемая) > 3°C  # аномально тепло
  AND RF_innsbruck < 40%  # сухо
```

---

## 8. Стек автоматизации (MVP)

### Уровень 1 — curl/Python (без зависимостей)
| Источник | Что берём |
|----------|-----------|
| Open-Meteo GFS | BL height, CAPE, CIN, LI, ветер по уровням, T профиль |
| Open-Meteo ICON-D2 | Hi-res: T, Td, ветер, порывы, облачность, осадки |
| Open-Meteo multi-model | Мультимодельное сравнение |
| GeoSphere TAWES | Текущие наблюдения (фён, ветер на станциях) |
| GeoSphere NWP | Прогноз с CAPE, облачность, снеговая линия |
| BrightSky | Простой DWD прогноз |
| RainViewer | Радар nowcast |
| DWD MOSMIX | Детальный прогноз по станциям (если нужна глубина) |

### Уровень 2 — Deno/headless (для спецданных)
| Источник | Что берём |
|----------|-----------|
| Meteo-Parapente | Сила термиков м/с, база hi-res |
| burnair | Thermikstärke по стартам |
| ALPTHERM | Thermikqualität (Австрия) |

### Уровень 3 — Справочные (ручные)
| Источник | Зачем |
|----------|-------|
| SkySight | Convergence zones, wave |
| XContest | Sanity-check: кто летал в похожих условиях |
| Foto-Webcam | Визуальный контроль облачности |

---

## 9. Ключевые координаты для запросов

| Район | Lat | Lon | Elevation | Peaks MSL | DWD ID | GeoSphere ID |
|-------|-----|-----|-----------|-----------|--------|---------------|
| Lenggries | 47.68 | 11.57 | 700 | 1800 | — | — |
| Wallberg | 47.64 | 11.79 | 1620 | 1722 | — | — |
| Kössen | 47.67 | 12.40 | 590 | 1900 | — | 11130 (Kufstein) |
| Innsbruck | 47.26 | 11.39 | 578 | 2600 | — | 11121/11320 |
| Greifenburg | 46.75 | 13.18 | 600 | 2800 | — | 11204 (Lienz) |
| Speikboden | 46.90 | 11.87 | 950 | 2500 | — | — |
| Bassano | 45.78 | 11.73 | 130 | 1700 | — | — |
| München (база) | 48.14 | 11.58 | 520 | — | 10865 | — |
