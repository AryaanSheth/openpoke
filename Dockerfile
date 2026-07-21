# One image, two run modes. `docker-compose.yml` (Phase 0) already builds both the `api`
# and `worker` services from this file and picks the mode via `command:` — nothing here
# needs to know which one it is. The same image also runs the pre-deploy migration step
# (`alembic upgrade head`) — see docs/DEPLOY.md for why that must run before either
# service starts, not on app boot.
FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependency layer first so code-only changes don't invalidate the pip install cache.
COPY server/requirements.txt server/requirements.txt
RUN pip install --no-cache-dir -r server/requirements.txt

# Application code. server/data/ (local file-store state, pre-Postgres) and any .env* are
# excluded by .dockerignore even though this COPY is broad — verified empirically, see
# docs/phase-6-notes.md.
COPY server/ server/
COPY alembic/ alembic/
COPY alembic.ini alembic.ini

# Non-root: FastAPI/uvicorn and the worker never need root.
RUN useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8001

# Default: API. `docker-compose.yml` overrides `command:` for the worker
# (`python -m server.worker`) and would do the same for a one-shot migration job
# (`alembic upgrade head`).
CMD ["python", "-m", "server.server"]
