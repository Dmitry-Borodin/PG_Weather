#!/usr/bin/env bash
# Запуск метео-триажа для XC closed routes.
#
# По умолчанию запускается через Docker (с headless Playwright).
# Дата по умолчанию — ближайшая суббота (или сегодня, если суббота).
#
# Использование:
#   ./run.sh                                   # Docker, след. суббота, все локации
#   ./run.sh 2025-07-15                        # Docker, конкретная дата
#   ./run.sh 2025-07-15 lenggries,koessen      # Docker, конкретные локации
#   ./run.sh 2025-07-15 all icon_d2,gfs meteo_parapente  # Docker, конкретные источники
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

# Build if image doesn't exist or Dockerfile changed
if ! docker image inspect "$IMAGE_NAME" &>/dev/null || \
   [[ "$(docker inspect --format='{{.Created}}' "$IMAGE_NAME" 2>/dev/null)" < \
      "$(stat -c %Y Dockerfile 2>/dev/null || stat -f %m Dockerfile)" ]]; then
    echo "Building Docker image..."
    docker build -t "$IMAGE_NAME" .
fi

mkdir -p reports

docker run --rm \
    -v "$(pwd)/reports:/app/reports" \
    "$IMAGE_NAME" \
    --date "$DATE" \
    --locations "$LOCATIONS" \
    --sources "$SOURCES" \
    --headless-sources "$HEADLESS"
