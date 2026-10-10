"""Test settings: a fixed environment, isolated from any local `.env`, then the regular settings."""

import os
import tempfile

TEST_ENV = {
    "DJANGO_SECRET_KEY": "test-only-secret-key",
    "DJANGO_DEBUG": "false",
    "ISCC_C2PA_STORE_URL": "https://store.example",
    "ISCC_C2PA_STORE_DATA_DIR": tempfile.mkdtemp(prefix="iscc-c2pa-store-tests-"),
    "ISCC_C2PA_STORE_SECKEY": "",
    "ISCC_C2PA_STORE_HUB_URL": "",
    "ISCC_C2PA_STORE_OPERATOR_TOKEN_SHA256": "",
    "ISCC_C2PA_STORE_NUM_PROXIES": "0",
    "ISCC_C2PA_STORE_RATE_WRITE": "1000/m",
    "ISCC_C2PA_STORE_RATE_READ": "1000/m",
    "DATABASE_URL": "sqlite://:memory:",
}
os.environ.update(TEST_ENV)

from iscc_c2pa_store.settings import *  # noqa: E402, F403

ALLOWED_HOSTS = ["store.example", "testserver"]
STORAGES["staticfiles"] = {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}  # noqa: F405
SERVESTATIC_AUTOREFRESH = SERVESTATIC_USE_FINDERS = True
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]  # fast test users; never in production
