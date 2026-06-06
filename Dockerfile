FROM python:3.12-slim
RUN apt-get update && apt-get install -y ffmpeg curl ca-certificates && \
    ARCH="$(uname -m)" && \
    case "$ARCH" in \
      aarch64|arm64) AWF_ARCH=linux-arm64 ;; \
      *) AWF_ARCH=linux-amd64 ;; \
    esac && \
    curl -fsSL "https://github.com/bbc/audiowaveform/releases/download/1.10.1/audiowaveform-1.10.1-${AWF_ARCH}.tar.gz" \
      | tar xz -C /tmp && \
    mv "/tmp/audiowaveform-1.10.1-${AWF_ARCH}/bin/audiowaveform" /usr/local/bin/audiowaveform && \
    chmod +x /usr/local/bin/audiowaveform && \
    rm -rf /var/lib/apt/lists/* /tmp/audiowaveform-*
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
