# Навигатор ПП РФ №719 — образ приложения (FastAPI + e5 + RAG).
# Qdrant запускается отдельным контейнером (см. docker-compose.yml).
FROM python:3.12-slim

# libgomp1 — OpenMP-рантайм для torch (e5 на CPU); curl — для healthcheck.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libgomp1 curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# CPU-сборка torch отдельно (иначе pip тянет CUDA-вариант ~2.5 ГБ). Затем — остальное.
# sentence-transformers увидит torch уже установленным и не переустановит.
COPY requirements.txt .
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu \
 && pip install --no-cache-dir -r requirements.txt

# Код + структурированная база знаний (structured/*.json для load_kb).
COPY . .

# Кэш HuggingFace/e5 и sentence-transformers — в /models (монтируется томом,
# чтобы модель качалась один раз и переживала пересборку).
ENV HF_HOME=/models \
    SENTENCE_TRANSFORMERS_HOME=/models \
    PYTHONUNBUFFERED=1

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8000/ping || exit 1

CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
