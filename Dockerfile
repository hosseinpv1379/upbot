FROM python:3.11-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg p7zip-full \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p downloads logs

VOLUME ["/app/downloads", "/app/logs", "/app/data"]

ENV DB_PATH=/app/data/uploader.db \
    DOWNLOAD_DIR=/app/downloads \
    LOG_DIR=/app/logs \
    PYTHONUNBUFFERED=1

CMD ["python", "-m", "uploader.main"]
