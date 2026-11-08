# The service image: the public cited-answers repository at a pinned commit, unchanged, plus
# OpenTelemetry auto-instrumentation (hash-pinned, compiled against the service's own lock).
# Built locally by scripts/build-image.sh; pushed to no registry. Every base image is pinned
# by digest.
FROM ghcr.io/astral-sh/uv:0.13.1@sha256:4a1f3666816dafadd20ceb71eac9a7d56d7ef30279f7d64048519ab901ada336 AS uv

FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /app
# Only what the service needs at run time: package metadata, source and the corpus. No tests,
# eval data, CI files or example configuration end up in the image.
COPY service/pyproject.toml service/uv.lock service/README.md service/LICENSE /app/
COPY service/src /app/src
COPY service/data/corpus /app/data/corpus
# Locked runtime dependencies only (no dev tools, no paid-provider SDKs).
RUN uv sync --frozen --no-dev
COPY otel-requirements.txt /tmp/otel-requirements.txt
RUN uv pip install --python /app/.venv/bin/python --require-hashes --no-deps \
        -r /tmp/otel-requirements.txt \
 && /app/.venv/bin/python -c "import cited_answers, opentelemetry.instrumentation.fastapi"

FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b
ARG SERVICE_REVISION=unknown
LABEL org.opencontainers.image.source="https://github.com/AfifiNL/cited-answers" \
      org.opencontainers.image.revision="${SERVICE_REVISION}" \
      org.opencontainers.image.licenses="Apache-2.0"
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin app
COPY --from=build /app /app
WORKDIR /app
# Offline and free by default: deterministic mock model, hashing embedder, in-memory store.
# Telemetry is off unless an OTLP endpoint is configured (the Helm chart does that).
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    CA_PROVIDER=mock CA_EMBEDDER=hash CA_STORE=memory CA_CACHE_DIR=/tmp/cache \
    OTEL_SERVICE_NAME=cited-answers OTEL_TRACES_EXPORTER=none OTEL_METRICS_EXPORTER=none \
    OTEL_LOGS_EXPORTER=none OTEL_PYTHON_EXCLUDED_URLS=healthz,readyz
USER 10001
EXPOSE 8000
ENTRYPOINT ["opentelemetry-instrument", "cited-answers", "serve", "--host", "0.0.0.0", "--port", "8000"]
