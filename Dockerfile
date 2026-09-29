# The workbench and everything it needs to print CVs, for linux/amd64 (the architecture CI tests).
# Data never lives in the image: mount it at /data (see compose.yaml).
FROM python:3.14-slim-trixie

# Chromium prints the CV to PDF. Liberation Sans has Arial's metrics, so English CVs lay out as
# on a Mac; Noto Sans CJK renders Chinese. tini reaps Chromium's child processes.
RUN apt-get update \
    && apt-get install -y --no-install-recommends chromium fonts-liberation fonts-noto-cjk tini ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin workbench \
    && mkdir /data && chown workbench:workbench /data

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Only what .dockerignore lets through: the code, the page, synthetic examples and the tests.
COPY . .

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    WORKBENCH_HOST=0.0.0.0 \
    WORKBENCH_PORT=8765 \
    WORKBENCH_DATA=/data \
    WORKBENCH_LOG_FORMAT=json \
    CHROME_PATH=/usr/bin/chromium

USER workbench
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import sys, urllib.request; sys.exit(urllib.request.urlopen('http://127.0.0.1:8765/healthz', timeout=4).status != 200)"]
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "web.py"]
