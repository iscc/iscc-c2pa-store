"""WSGI entry point for gunicorn."""

import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "iscc_c2pa_store.settings")
application = get_wsgi_application()
