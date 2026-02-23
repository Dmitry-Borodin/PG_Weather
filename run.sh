#!/usr/bin/env bash
# Запуск метео-триажа для XC closed routes.
#
# Использование:
#   ./run.sh 2025-07-15                        # все локации, все источники
#   ./run.sh 2025-07-15 lenggries,koessen      # конкретные локации
#   ./run.sh 2025-07-15 all icon_d2,gfs        # конкретные источники

set -euo pipefail
cd "$(dirname "$0")"

DATE="${1:?Usage: ./run.sh DATE [LOCATIONS] [SOURCES]}"
LOCATIONS="${2:-all}"
SOURCES="${3:-all}"

python3 scripts/fetch_weather.py \
    --date "$DATE" \
    --locations "$LOCATIONS" \
    --sources "$SOURCES"
