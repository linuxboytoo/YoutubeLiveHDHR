FROM python:3.12-slim

WORKDIR /app

# Install system deps for yt-dlp/streamlink
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    nodejs \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

ENV CONFIG_PATH=/config/channels.yaml \
    HDHR_DEVICE_ID=12345678 \
    HDHR_FRIENDLY_NAME=YouTubeHDHR \
    POLL_INTERVAL=60 \
    PORT=5004

EXPOSE 5004

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "5004"]
