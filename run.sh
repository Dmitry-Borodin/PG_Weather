#!/usr/bin/env bash
# Запуск метео-триажа для XC closed routes (v2.0).
#
# Модельный стек (fallback chains):
#   ICON:  D2 2km → EU 7km → Global 13km
#   ECMWF: IFS 0.25° → IFS 0.4°
#   GFS:   Seamless (always, for BL/LI/CIN)
#   + ECMWF ENS + ICON-EU EPS + GeoSphere AROME + MOSMIX
#
# Модули: fetch_weather.py → fetchers.py / analysis.py / report.py
#
# По умолчанию запускается через Docker (с headless Playwright).
# Дата по умолчанию — ближайшая суббота (или сегодня, если суббота).
#
# Использование:
#   ./run.sh                                   # Docker, след. суббота, все локации
#   ./run.sh 2025-07-15                        # Docker, конкретная дата
#   ./run.sh 2025-07-15 lenggries,koessen      # Docker, конкретные локации
#   ./run.sh 2025-07-15 all ecmwf_hres,icon_d2 meteo_parapente  # Docker, конкретные источники
#
# Локальный режим (без Docker, без headless):
#   ./run.sh --local                           # след. суббота, без Docker
#   ./run.sh --local 2025-07-15                # конкретная дата, без Docker
#
# Интерактивный режим (shell внутри Docker-контейнера):
#   ./run.sh --shell                           # bash в контейнере с монтированными reports/

set -euo pipefail
cd "$(dirname "$0")"

IMAGE_NAME="pg-weather-triage"

# ── Compute next Saturday (or today if Saturday) ──
next_saturday() {
    local dow
    dow=$(date +%u)  # 1=Mon ... 7=Sun
    if [[ "$dow" -eq 6 ]]; then
        date +%Y-%m-%d
    else
        local days_until=$(( (6 - dow) % 7 ))
        [[ "$days_until" -eq 0 ]] && days_until=7
        date -d "+${days_until} days" +%Y-%m-%d 2>/dev/null \
            || date -v+${days_until}d +%Y-%m-%d  # macOS fallback
    fi
}

# ── Local mode (no headless) ──
if [[ "${1:-}" == "--local" ]]; then
    shift
    DATE="${1:-$(next_saturday)}"
    LOCATIONS="${2:-all}"
    SOURCES="${3:-all}"

    python3 scripts/fetch_weather.py \
        --date "$DATE" \
        --locations "$LOCATIONS" \
        --sources "$SOURCES" \
        --no-scraper
    exit $?
fi

# Docker preflight checks
if ! command -v docker >/dev/null 2>&1; then
    echo "Docker is not installed or not in PATH."
    echo "Use local mode instead: ./run.sh --local [DATE] [LOCATIONS] [SOURCES]"
    exit 127
fi

if ! docker info >/dev/null 2>&1; then
    echo "Docker daemon is not reachable."
    echo "Start Docker Desktop/daemon, or use local mode: ./run.sh --local"
    exit 1
fi

# ── Shell mode (interactive bash inside Docker) ──
if [[ "${1:-}" == "--shell" ]]; then
    shift

    echo "Building Docker image..."
    DOCKER_BUILDKIT=1 docker build -t "$IMAGE_NAME" .

    mkdir -p reports

    echo "Starting interactive shell in container..."
    echo "  reports/ is mounted at /app/reports"
    echo "  Run: python3 scripts/fetch_weather.py --help"
    docker run --rm -it \
        -v "$(pwd)/reports:/app/reports" \
        --entrypoint /bin/bash \
        "$IMAGE_NAME"
    exit $?
fi

# ── Docker mode (default) ──
DATE="${1:-$(next_saturday)}"
LOCATIONS="${2:-all}"
SOURCES="${3:-all}"
HEADLESS="${4:-meteo_parapente}"

echo "Building Docker image..."
DOCKER_BUILDKIT=1 docker build -t "$IMAGE_NAME" .

mkdir -p reports

docker run --rm \
    -v "$(pwd)/reports:/app/reports" \
    "$IMAGE_NAME" \
    --date "$DATE" \
    --locations "$LOCATIONS" \
    --sources "$SOURCES" \
    --headless-sources "$HEADLESS"
