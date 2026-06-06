FROM python:3.12-slim

# Minimal ffmpeg only — no audiowaveform (peaks use ffmpeg fallback).
# --no-install-recommends avoids GTK/Mesa/SDL (~200 extra packages) that slow Railway builds.
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        ffmpeg \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
