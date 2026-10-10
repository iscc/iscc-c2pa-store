# syntax=docker/dockerfile:1

# Build stage: install the locked runtime dependencies and the package into a virtual environment, collect static files.
FROM python:3.13-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.12.23 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_NO_CACHE=1 UV_PYTHON_DOWNLOADS=never DJANGO_STATIC_ROOT=/app/staticfiles
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-default-groups --no-install-project
COPY manage.py ./
COPY docker ./docker
COPY iscc_c2pa_store ./iscc_c2pa_store
RUN uv sync --frozen --no-default-groups --no-editable \
    && DJANGO_SECRET_KEY=build ISCC_C2PA_STORE_URL=http://build .venv/bin/python manage.py collectstatic --noinput

# Runtime stage: the virtual environment, static files and entrypoint, run by an unprivileged user.
FROM python:3.13-slim
RUN useradd --system --uid 10001 --no-create-home store && mkdir /data && chown store /data
COPY --from=build /app/.venv /app/.venv
COPY --from=build /app/staticfiles /app/staticfiles
COPY --from=build /app/manage.py /app/manage.py
COPY --from=build /app/docker/entrypoint.sh /app/entrypoint.sh
# GUNICORN_CMD_ARGS holds the server defaults so that a deployment can override any of them. One worker process
# with threads keeps the per-process rate limit counters in one place.
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    DJANGO_STATIC_ROOT=/app/staticfiles \
    ISCC_C2PA_STORE_DATA_DIR=/data \
    GUNICORN_CMD_ARGS="--bind 0.0.0.0:8000 --workers 1 --threads 8 --timeout 30 --no-control-socket --access-logfile - --access-logformat '%(h)s %({x-forwarded-for}i)s %(t)s \"%(r)s\" %(s)s %(b)s %(M)sms'"
USER store
WORKDIR /app
VOLUME /data
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2)"]
ENTRYPOINT ["/bin/sh", "/app/entrypoint.sh"]
