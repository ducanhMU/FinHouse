# syntax=docker/dockerfile:1.7
#
# BuildKit pip cache mount: partial downloads survive across rebuilds.
# A network drop mid-download (common with VN→US for the ~750MB torch
# CUDA wheel) won't waste what's already pulled — next `docker compose
# build` resumes from the cached partial.
#
# Trade-off: we drop `--no-cache-dir` so pip writes into the mounted
# /root/.cache/pip. The cache lives in BuildKit, NOT in the final
# image, so the image size is unchanged.

FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl && \
    rm -rf /var/lib/apt/lists/*

# Stage 1 — torch (heaviest layer, ~750MB CUDA wheel from US S3).
# If this fails mid-download, just `docker compose build finhouse-bge-m3`
# again — the BuildKit cache mount keeps the partial wheel.
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --upgrade pip && \
    pip install \
        --timeout 600 \
        --retries 10 \
        --resume-retries 10 \
        --index-url https://download.pytorch.org/whl/cu121 \
        --extra-index-url https://pypi.org/simple \
        torch==2.6.0

# Stage 2 — pure-Python deps (small, fast).
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install \
        --timeout 600 \
        --retries 10 \
        --resume-retries 10 \
        fastapi==0.115.6 \
        "uvicorn[standard]==0.34.0" \
        sentence-transformers==3.3.1 \
        FlagEmbedding==1.3.4 \
        peft==0.13.2 \
        numpy

COPY main.py .

EXPOSE 8081

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8081"]
