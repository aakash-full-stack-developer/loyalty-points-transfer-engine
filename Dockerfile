# syntax=docker/dockerfile:1
# One image is used by the api, worker and partner-simulator services (different commands).
# The "dev" target adds test/lint tooling and is used by the `tools` compose service.

# ---------------------------------------------------------------- base
FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_DEFAULT_TIMEOUT=120 \
    PIP_RETRIES=10 \
    VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH

# ---------------------------------------------------------------- deps
# Dependencies are installed from pyproject.toml only, so source changes don't bust the cache.
# The pip cache is a BuildKit cache mount: it speeds up rebuilds but never enters the image.
FROM base AS deps
WORKDIR /build
RUN python -m venv /opt/venv
COPY pyproject.toml ./
RUN --mount=type=cache,target=/root/.cache/pip \
    python -c "import tomllib; d = tomllib.load(open('pyproject.toml', 'rb')); \
print('\n'.join(d['project']['dependencies']))" > requirements.txt \
    && pip install -r requirements.txt

FROM deps AS dev-deps
RUN --mount=type=cache,target=/root/.cache/pip \
    python -c "import tomllib; d = tomllib.load(open('pyproject.toml', 'rb')); \
print('\n'.join(d['project']['optional-dependencies']['dev']))" > requirements-dev.txt \
    && pip install -r requirements-dev.txt

# ---------------------------------------------------------------- dev (tests, lint, migrations)
FROM base AS dev
RUN groupadd --system app && useradd --system --gid app --home-dir /app app
WORKDIR /app
COPY --from=dev-deps /opt/venv /opt/venv
COPY . .
USER app

# ---------------------------------------------------------------- runtime
FROM base AS runtime
RUN groupadd --system app && useradd --system --gid app --home-dir /app app
WORKDIR /app
COPY --from=deps /opt/venv /opt/venv
COPY alembic.ini ./
COPY app ./app
COPY partner_simulator ./partner_simulator
COPY migrations ./migrations
COPY scripts ./scripts
USER app
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
