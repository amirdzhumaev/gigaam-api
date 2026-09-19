FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock
RUN useradd --uid 10001 --create-home app \
    && mkdir -p /data /app/data && chown -R app:app /data /app/data

FROM base AS api
COPY pyproject.toml ./
COPY src ./src
COPY alembic.ini ./
COPY migrations ./migrations
RUN pip install --no-cache-dir --no-deps .
USER app
CMD ["uvicorn", "gigaam_api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8100", "--no-access-log"]

FROM base AS worker
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
COPY requirements-worker.lock ./
RUN pip install --no-cache-dir -r requirements-worker.lock && mkdir -p /models && chown app:app /models
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir --no-deps .
ENV HF_HOME=/models
USER app
CMD ["python", "-m", "gigaam_api.worker"]
