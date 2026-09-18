FROM python:3.12-slim AS api
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock
COPY pyproject.toml ./
COPY src ./src
COPY alembic.ini ./
COPY migrations ./migrations
RUN pip install --no-cache-dir --no-deps . && useradd --uid 10001 --create-home app \
    && mkdir -p /data /app/data && chown -R app:app /data /app/data
USER app
CMD ["uvicorn", "gigaam_api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8100", "--no-access-log"]

FROM api AS worker
USER root
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
COPY requirements-worker.lock ./
RUN pip install --no-cache-dir -r requirements-worker.lock && mkdir -p /models && chown app:app /models
ENV HF_HOME=/models
USER app
CMD ["python", "-m", "gigaam_api.worker"]
