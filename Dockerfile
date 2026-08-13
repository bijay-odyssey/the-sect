# sect-core. One process, one Postgres, no sidecars.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# pyproject declares license-files and readme, so the build needs both present.
COPY pyproject.toml LICENSE README.md ./
COPY src ./src

# The core extra only. No CLI, no dev tooling, nothing a server does not run.
RUN pip install --no-cache-dir ".[core]"

RUN useradd --create-home --uid 10001 sect
USER sect

EXPOSE 8000

# Migrations run on boot behind an advisory lock (SECT_AUTO_MIGRATE, default true), so
# a deploy needs no release phase. One worker: the free tier has little memory, and a
# second worker would buy nothing when Postgres is doing all the coordination.
CMD ["sh", "-c", "exec uvicorn sect.core.app:create_app --factory --host 0.0.0.0 --port ${PORT:-8000} --workers 1 --timeout-keep-alive 65"]
