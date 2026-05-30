# syntax=docker/dockerfile:1.7

FROM node:24-bookworm-slim@sha256:24dc26ef1e3c3690f27ebc4136c9c186c3133b25563ae4d7f0692e4d1fe5db0e AS frontend

WORKDIR /src
COPY frontend/package*.json ./frontend/
WORKDIR /src/frontend
RUN npm ci

COPY frontend/ ./
RUN npm run build

FROM python:3.11-slim@sha256:9a7765b36773a37061455b332f18e265e7f58f6fea9c419a550d2a8b0e9db834 AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000

WORKDIR /app/backend

RUN useradd --create-home --shell /usr/sbin/nologin appuser

COPY backend/ /app/backend/
COPY --from=frontend /src/backend/src/api/static/react /app/backend/src/api/static/react

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.lock \
    && pip install --no-cache-dir --no-deps -e ".[api]"

USER appuser

EXPOSE 8000

CMD ["sh", "-c", "uvicorn src.api.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
