#!/usr/bin/env bash
# Запуск метео-триажа для XC closed routes (v1.0).
#
# Модельный стек: ECMWF HRES + ICON Seamless + ICON-D2 + GFS
#   + ECMWF ENS + ICON-EU EPS + GeoSphere AROME + MOSMIX
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

# ── Docker mode (default) ──
DATE="${1:-$(next_saturday)}"
LOCATIONS="${2:-all}"
SOURCES="${3:-all}"
HEADLESS="${4:-meteo_parapente}"

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
