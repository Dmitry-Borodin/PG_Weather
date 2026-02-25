# Источники данных для метео-триажа

**Версия:** 2.0

---

## Модельный стек

### Горизонт D-5…D-1 (среднесрок)

| Модель | Эндпоинт | Разрешение | Роль |
|--------|----------|------------|------|
| **ECMWF IFS HRES** | Open-Meteo `/v1/forecast?models=ecmwf_ifs025` | 0.25° | Опорный скелет: ветер, влажность, облачность, CAPE |
| **ICON Seamless** | Open-Meteo `/v1/forecast?models=icon_seamless` | blend | Дополнение ECMWF; до 5 дней (вместо icon_d2) |
| **GFS** | Open-Meteo `/v1/gfs?models=gfs_seamless` | 0.25° global | Единственная с BL height, LI, CIN |
| **ECMWF ENS** | `ensemble-api…?models=ecmwf_ifs025` | 51 member | p10/p50/p90/spread |
| **ICON-EU EPS** | `ensemble-api…?models=icon_eu` | 40 member | p10/p50/p90/spread |

### Горизонт D-2…D-0 (ближний)

| Модель | Эндпоинт | Разрешение | Роль |
|--------|----------|------------|------|
| **ICON-D2** | Open-Meteo `/v1/dwd-icon?models=icon_d2` | 2 км | Локальный hi-res override (0–48ч) |
| **GeoSphere AROME** | `dataset.api.hub.geosphere.at/…/nwp-v1-1h-2500m` | 2.5 км | Хай-рез, **полный ряд по окну** |
| **DWD MOSMIX** | `opendata.dwd.de/…/MOSMIX_L_LATEST_*.kmz` | station | Точечный sanity-check, **локальное время** |

### Убрано

| Источник | Причина |
|----------|---------|
| ~~BrightSky @13 forecast~~ | Дублировал MOSMIX без pressure-level / CAPE |

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
    "head": [2.1, 3.0],    // первые 2 часа
    "tail": [5.2, 4.8],    // последние 2 часа
    "trend": "rising"       // rising/stable/falling
  }
}
```

### Ансамбли
```json
{
  "param_p10": ..., "param_p50": ..., "param_p90": ..., "param_spread": ...
}
```

---

## 1. Open-Meteo — ОСНОВНОЙ ИСТОЧНИК

### 1.1 ECMWF IFS HRES (deterministic, base model)

```bash
curl -s "https://api.open-meteo.com/v1/forecast?latitude=47.68&longitude=11.57\
  &hourly=temperature_2m,dewpoint_2m,relative_humidity_2m,\
  windspeed_10m,windgusts_10m,winddirection_10m,\
  cloudcover,cloudcover_low,cloudcover_mid,cloudcover_high,\
  precipitation,cape,\
  shortwave_radiation,direct_radiation,sunshine_duration,\
  temperature_850hPa,temperature_700hPa,\
  relative_humidity_850hPa,relative_humidity_700hPa,\
  windspeed_850hPa,winddirection_850hPa,\
  windspeed_700hPa,winddirection_700hPa\
  &models=ecmwf_ifs025&timezone=Europe/Berlin&windspeed_unit=ms\
  &start_date=DATE&end_date=DATE"
```

### 1.2 ICON Seamless (расширен до 5 дней)

Тот же набор параметров, `models=icon_seamless`. Заменяет icon_d2 для горизонта > 48ч.

### 1.3 ICON-D2 (2 км, только 0–48ч)

`models=icon_d2` через `/v1/dwd-icon`. Локальный override на короткий горизонт.

### 1.4 GFS (единственная с BL height, LI, CIN)

Дополнительно: `boundary_layer_height,convective_inhibition,lifted_index,temperature_500hPa`.

### 1.5 Ансамбли (ECMWF ENS + ICON-EU EPS)

```bash
curl -s "https://ensemble-api.open-meteo.com/v1/ensemble?latitude=47.68&longitude=11.57\
  &hourly=temperature_2m,windspeed_10m,windgusts_10m,cloudcover,precipitation,cape,windspeed_850hPa\
  &models=ecmwf_ifs025&timezone=Europe/Berlin&windspeed_unit=ms\
  &start_date=DATE&end_date=DATE"
```
Ответ: `{param}_member00`, …, `{param}_member50`. Агрегация → p10/p50/p90/spread.

### 1.6 Извлекаемые переменные

| Параметр | Ключ | Модели | Назначение |
|----------|------|--------|------------|
| T 2m, Td 2m | temperature_2m, dewpoint_2m | Все | LCL / база |
| RH 2m / 850 / 700 | relative_humidity_* | ECMWF, ICON, GFS | Влажность, фён |
| Ветер 10m + порывы | windspeed_10m, windgusts_10m | Все | Приземный ветер |
| Ветер 850/700 hPa | windspeed_850/700hPa | ECMWF, ICON, GFS | Фоновый ветер / фён |
| Облачность low/mid/high | cloudcover_low/mid/high | Все | Прогрев + инверсии |
| BL height | boundary_layer_height | **GFS only** | Глубина перемешивания |
| CAPE / CIN / LI | cape, convective_inhibition, lifted_index | GFS (+CAPE все) | Нестабильность |
| SW / direct radiation | shortwave_radiation, direct_radiation | ECMWF, ICON, GFS | Прогрев |
| Осадки | precipitation | Все | Осадки |

### 1.7 Расчёты

LCL: `125 × (T_2m − Td_2m) + elevation`
Lapse rate: `(T_850 − T_700) / 1.5 °C/km` (>7 сильная, >8 очень сильная)
W*: Deardorff `(g/T_K × BL_h × 0.4·SWR / (ρ·cp))^(1/3)` (<0.5 слабо, 1.5+ хорошо, 2.5+ сильно)

---

## 2. GeoSphere AROME 2.5 km — полный ряд по окну

```bash
curl -s "https://dataset.api.hub.geosphere.at/v1/timeseries/forecast/nwp-v1-1h-2500m\
  ?parameters=t2m,cape,cin,tcc,lcc,mcc,hcc,u10m,v10m,ugust,vgust,snowlmt,rr,grad\
  &lat_lon=47.26,11.39&output_format=geojson"
```

Маппинг: t2m→temperature_2m, tcc→cloudcover, lcc/mcc/hcc→low/mid/high, grad→shortwave_radiation, rr→precipitation.
Timestamps в **UTC** → конвертируются в local через `zoneinfo`.

### 2.1 Наблюдения TAWES

Как прежде (станции 11320, 11121, 11130, 11204, 11279, 11144).

---

## 3. DWD MOSMIX — sanity check, локальное время

KMZ timestamps в UTC → скрипт конвертирует в **Europe/Berlin** с учётом DST.
Параметры: TTT, Td, FF, FX1, DD, N/Neff/Nh/Nm/Nl, SunD1, Rad1h, RR1c.

---

## 4. Фён

Индикаторы: ветер 700 hPa > 10 м/с южный + RH700 < 30% + аномальный lapse rate.
Станции: Innsbruck Airport (11121), snowlmt GeoSphere.

---

## 5. Ключевые координаты

| Район | Lat | Lon | Elev | Peaks | MOSMIX | GeoSphere |
|-------|-----|-----|------|-------|--------|-----------|
| Lenggries | 47.68 | 11.57 | 700 | 1800 | 10963 | — |
| Wallberg | 47.64 | 11.79 | 1620 | 1722 | 10963 | — |
| Kössen | 47.67 | 12.40 | 590 | 1900 | — | 11130 |
| Innsbruck | 47.26 | 11.39 | 578 | 2600 | 11120 | 11121 |
| Greifenburg | 46.75 | 13.18 | 600 | 2800 | — | 11204 |
| Speikboden | 46.90 | 11.87 | 950 | 2500 | — | — |
| Bassano | 45.78 | 11.73 | 130 | 1700 | — | — |
| München | 48.14 | 11.58 | 520 | — | 10865 | — |
