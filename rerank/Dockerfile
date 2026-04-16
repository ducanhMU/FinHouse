FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl && \
    rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir \
        --timeout 600 \
        --retries 10 \
        --resume-retries 10 \
        --index-url https://download.pytorch.org/whl/cu121 \
        --extra-index-url https://pypi.org/simple \
        torch==2.5.1

RUN pip install --no-cache-dir \
        --timeout 600 \
        --retries 10 \
        --resume-retries 10 \
        fastapi==0.115.6 \
        "uvicorn[standard]==0.34.0" \
        sentence-transformers==3.3.1 \
        numpy

COPY main.py .

EXPOSE 8082

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8082"]