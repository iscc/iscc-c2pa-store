#!/bin/sh
# Container entrypoint: apply migrations, retry pending declarations every ISCC_C2PA_STORE_RETRY_SECONDS in the
# background, and serve the app with gunicorn in the foreground. Server options come from GUNICORN_CMD_ARGS (defaults
# in the Dockerfile).
set -eu

python manage.py migrate --noinput

(
  set +e  # a failing round or a bad interval must not end the loop
  while true; do
    sleep "${ISCC_C2PA_STORE_RETRY_SECONDS:-120}" || sleep 120
    python manage.py retry_declarations || echo "retry_declarations failed" >&2
  done
) &

exec gunicorn iscc_c2pa_store.wsgi
