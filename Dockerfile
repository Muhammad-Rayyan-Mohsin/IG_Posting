# Stage 1: Build dependencies (includes compilers, removed in final image)
FROM python:3.11-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# Pre-download faster-whisper model into the image layer (~74MB)
# This avoids a 30-60s download on every Railway cron run.
ENV PYTHONPATH=/install/lib/python3.11/site-packages
RUN python -c "from faster_whisper import WhisperModel; WhisperModel('base', device='cpu', compute_type='int8')"

# Stage 2: Runtime (no compilers, smaller image)
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libsndfile1 \
    libmagic1 \
    fonts-noto-cjk \
    fonts-noto-color-emoji \
    fonts-freefont-ttf \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy pip packages from builder
COPY --from=builder /install /usr/local

# Copy faster-whisper model cache from builder
COPY --from=builder /root/.cache/huggingface /root/.cache/huggingface

# Copy the project
COPY . .

CMD ["python", "src/main.py"]
