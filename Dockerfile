FROM docker.io/denoland/deno:debian-2.1.4

# Python 3 + Chromium system deps (fonts, libs)
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      python3 python3-venv ca-certificates \
      fonts-liberation fonts-noto-color-emoji fonts-unifont \
      libasound2 libatk-bridge2.0-0 libatk1.0-0 libcairo2 libcups2 \
      libdbus-1-3 libdrm2 libgbm1 libglib2.0-0 libnspr4 libnss3 \
      libpango-1.0-0 libx11-6 libxcb1 libxcomposite1 libxdamage1 \
      libxext6 libxfixes3 libxkbcommon0 libxrandr2 xvfb && \
    rm -rf /var/lib/apt/lists/*

ENV PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers
ENV DENO_DIR=/deno-dir

WORKDIR /app

# Copy scraper first to cache deps + install Chromium (heavy layer)
COPY scripts/scraper.ts scripts/scraper.ts
RUN deno cache scripts/scraper.ts && \
    deno run -A npm:playwright@1.52.0 install chromium

# Copy the rest of the project
COPY . .

# Default entrypoint: pass --date and other flags
ENTRYPOINT ["python3", "scripts/fetch_weather.py"]
