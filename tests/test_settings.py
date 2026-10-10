"""Tests of the environment helpers of the settings module and of the settings they derive."""

import runpy

import pytest
from django.conf import settings
from django.shortcuts import resolve_url
from django.test import RequestFactory

from iscc_c2pa_store.settings import env, env_bool, env_list, hub_id

NAME = "ISCC_C2PA_STORE_TEST_SETTING"


def test_env_reads_variable(monkeypatch):
    monkeypatch.setenv(NAME, "value")
    assert env(NAME) == "value"
    assert env(NAME, "default") == "value"


def test_env_default(monkeypatch):
    monkeypatch.delenv(NAME, raising=False)
    assert env(NAME, "default") == "default"


def test_env_empty_value_is_kept(monkeypatch):
    monkeypatch.setenv(NAME, "")
    assert env(NAME, "default") == ""


def test_env_missing_without_default_stops(monkeypatch):
    monkeypatch.delenv(NAME, raising=False)
    with pytest.raises(RuntimeError, match=f"Set the {NAME} environment variable"):
        env(NAME)


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "Yes", "on", " on "])
def test_env_bool_true(monkeypatch, value):
    monkeypatch.setenv(NAME, value)
    assert env_bool(NAME, False) is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "", "maybe"])
def test_env_bool_false(monkeypatch, value):
    monkeypatch.setenv(NAME, value)
    assert env_bool(NAME, True) is False


@pytest.mark.parametrize("default", [True, False])
def test_env_bool_default(monkeypatch, default):
    monkeypatch.delenv(NAME, raising=False)
    assert env_bool(NAME, default) is default


def test_env_list_splits_and_strips(monkeypatch):
    monkeypatch.setenv(NAME, " a.example , b.example,,  ")
    assert env_list(NAME, ["ignored"]) == ["a.example", "b.example"]


def test_env_list_default(monkeypatch):
    monkeypatch.delenv(NAME, raising=False)
    assert env_list(NAME, ["a", "b"]) == ["a", "b"]
    assert env_list(NAME, []) == []


def test_derived_settings():
    assert settings.ISCC_C2PA_STORE_URL == "https://store.example"
    assert settings.ISCC_C2PA_STORE_HOST == "store.example"
    assert settings.ISCC_C2PA_STORE_CONTROLLER == "did:web:store.example"
    assert settings.PUBLIC_HOSTNAME == "store.example"
    assert settings.CSRF_TRUSTED_ORIGINS == ["https://store.example"]
    assert settings.SECURE is True
    assert settings.SECURE_PROXY_SSL_HEADER is None  # no trusted proxy in the test environment
    assert settings.DATA_UPLOAD_MAX_MEMORY_SIZE == settings.ISCC_C2PA_STORE_MAX_UPLOAD


def load_settings(monkeypatch, **environ):
    # type: (object, str) -> dict
    """Run the settings module with extra environment variables and return its names."""
    for key, value in environ.items():
        monkeypatch.setenv(key, value)
    return runpy.run_path(str(settings.BASE_DIR / "iscc_c2pa_store" / "settings.py"))


def without(monkeypatch, *names):
    # type: (pytest.MonkeyPatch, str) -> None
    """Remove environment variables, so that the settings module falls back to its defaults."""
    for key in names:
        monkeypatch.delenv(key, raising=False)


def test_allowed_hosts_default_to_public_host_and_loopback(monkeypatch):
    without(monkeypatch, "DJANGO_ALLOWED_HOSTS")
    assert load_settings(monkeypatch)["ALLOWED_HOSTS"] == ["store.example", "localhost", "127.0.0.1"]


def test_configured_allowed_hosts_keep_loopback(monkeypatch):
    # The container health check requests http://127.0.0.1:8000/healthz whatever the operator sets.
    loaded = load_settings(monkeypatch, DJANGO_ALLOWED_HOSTS="c2pa-store.iscc.io, other.example")
    assert loaded["ALLOWED_HOSTS"] == ["c2pa-store.iscc.io", "other.example", "localhost", "127.0.0.1"]


def test_limit_defaults(monkeypatch):
    names = ["ISCC_C2PA_STORE_RATE_LOGIN", "ISCC_C2PA_STORE_UNTHROTTLED", "ISCC_C2PA_STORE_MATCH_THRESHOLD"]
    without(monkeypatch, "ISCC_C2PA_STORE_RATE_WRITE", "ISCC_C2PA_STORE_RATE_READ", *names)
    loaded = load_settings(monkeypatch)
    assert loaded["ISCC_C2PA_STORE_RATE_WRITE"] == "10/m"
    assert loaded["ISCC_C2PA_STORE_RATE_READ"] == "120/m"
    assert loaded["ISCC_C2PA_STORE_RATE_LOGIN"] == "10/m"
    assert loaded["ISCC_C2PA_STORE_UNTHROTTLED"] == []
    assert loaded["ISCC_C2PA_STORE_MATCH_THRESHOLD"] == 75  # the lowest score iscc-c2pa-resolver reports


def test_limits_from_environment(monkeypatch):
    loaded = load_settings(
        monkeypatch,
        ISCC_C2PA_STORE_RATE_LOGIN="3/h",
        ISCC_C2PA_STORE_UNTHROTTLED="203.0.113.9, 2001:db8::9",
        ISCC_C2PA_STORE_MATCH_THRESHOLD="80",
    )
    assert loaded["ISCC_C2PA_STORE_RATE_LOGIN"] == "3/h"
    assert loaded["ISCC_C2PA_STORE_UNTHROTTLED"] == ["203.0.113.9", "2001:db8::9"]
    assert loaded["ISCC_C2PA_STORE_MATCH_THRESHOLD"] == 80


def test_login_urls_point_to_the_admin():
    assert resolve_url(settings.LOGIN_URL) == "/admin/login/"
    assert resolve_url(settings.LOGIN_REDIRECT_URL) == "/admin/"


def test_proxy_settings_trust_forwarded_headers(monkeypatch):
    loaded = load_settings(monkeypatch, ISCC_C2PA_STORE_NUM_PROXIES="1")
    assert loaded["NINJA_NUM_PROXIES"] == 1
    assert loaded["SECURE_PROXY_SSL_HEADER"] == ("HTTP_X_FORWARDED_PROTO", "https")


def test_no_proxy_settings_ignore_forwarded_headers(monkeypatch):
    loaded = load_settings(monkeypatch, ISCC_C2PA_STORE_NUM_PROXIES="0")
    assert loaded["NINJA_NUM_PROXIES"] == 0
    assert loaded["SECURE_PROXY_SSL_HEADER"] is None


def test_request_behind_proxy_is_secure(settings):
    settings.SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    assert RequestFactory().get("/", HTTP_X_FORWARDED_PROTO="https").is_secure()
    assert not RequestFactory().get("/", HTTP_X_FORWARDED_PROTO="http").is_secure()


def test_gunicorn_leaves_forwarded_headers_to_django():
    entrypoint = (settings.BASE_DIR / "docker" / "entrypoint.sh").read_text(encoding="utf-8")
    assert "exec gunicorn iscc_c2pa_store.wsgi" in entrypoint
    assert "forwarded-allow-ips" not in entrypoint


def test_gunicorn_options_come_from_the_environment():
    # Options on the command line would override GUNICORN_CMD_ARGS, so the entrypoint passes none.
    entrypoint = (settings.BASE_DIR / "docker" / "entrypoint.sh").read_text(encoding="utf-8")
    assert entrypoint.rstrip().endswith("exec gunicorn iscc_c2pa_store.wsgi")
    runtime = (settings.BASE_DIR / "Dockerfile").read_text(encoding="utf-8").rpartition("\nFROM ")[2]
    assert 'GUNICORN_CMD_ARGS="--bind 0.0.0.0:8000' in runtime


def test_static_root_from_environment(monkeypatch, tmp_path):
    # The image collects static files to /app/staticfiles and sets DJANGO_STATIC_ROOT to it in both stages.
    assert load_settings(monkeypatch, DJANGO_STATIC_ROOT=str(tmp_path))["STATIC_ROOT"] == tmp_path
    runtime = (settings.BASE_DIR / "Dockerfile").read_text(encoding="utf-8").rpartition("\nFROM ")[2]
    assert "DJANGO_STATIC_ROOT=/app/staticfiles" in runtime


def test_operator_sidebar_links_resolve():
    sidebar = settings.UNFOLD["SIDEBAR"]
    links = [str(item["link"]) for group in sidebar["navigation"] for item in group["items"]]
    assert links == [
        "/admin/",
        "/admin/iscc_c2pa_store/manifest/",
        "/admin/iscc_c2pa_store/declaration/",
        "/admin/auth/user/",
        "/admin/auth/group/",
    ]


@pytest.mark.parametrize(("text", "number"), [("0", 0), ("1", 1), ("4095", 4095)])
def test_hub_id(text, number):
    assert hub_id(text) == number


@pytest.mark.parametrize("text", ["-1", "4096", "70000"])
def test_hub_id_out_of_range(text):
    with pytest.raises(ValueError, match="0 to 4095"):
        hub_id(text)
