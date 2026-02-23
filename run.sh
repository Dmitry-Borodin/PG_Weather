#!/usr/bin/env bash
# Запуск метео-триажа для XC closed routes.
#
# Использование (локально, без headless):
#   ./run.sh 2025-07-15                        # все локации, все источники
#   ./run.sh 2025-07-15 lenggries,koessen      # конкретные локации
#   ./run.sh 2025-07-15 all icon_d2,gfs        # конкретные источники
#
# Использование (Docker, c headless Playwright):
#   ./run.sh --docker 2025-07-15
#   ./run.sh --docker 2025-07-15 all all meteo_parapente,xccontest

set -euo pipefail
cd "$(dirname "$0")"

IMAGE_NAME="pg-weather-triage"

# ── Docker mode ──
if [[ "${1:-}" == "--docker" ]]; then
    shift
    DATE="${1:?Usage: ./run.sh --docker DATE [LOCATIONS] [SOURCES] [HEADLESS_SOURCES]}"
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
    exit $?
fi

# ── Local mode (no headless) ──
DATE="${1:?Usage: ./run.sh DATE [LOCATIONS] [SOURCES]  |  ./run.sh --docker DATE ...}"
LOCATIONS="${2:-all}"
SOURCES="${3:-all}"

python3 scripts/fetch_weather.py \
    --date "$DATE" \
    --locations "$LOCATIONS" \
    --sources "$SOURCES" \
    --no-scraper
