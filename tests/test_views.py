"""Tests of the pages for people: landing page, API reference, OpenAPI files and the health check."""

import json

import pytest
import yaml
from conftest import iscc_manifest, upload
from django.core.cache import cache
from django.test import RequestFactory

from iscc_c2pa_store import __version__, urls, views
from iscc_c2pa_store.views import OPENAPI_DIR


@pytest.mark.django_db
def test_index_renders_host_and_counts(client):
    assert upload(client, iscc_manifest()).status_code == 200
    response = client.get("/")
    assert response.status_code == 200
    assert response.context["host"] == "store.example"
    assert response.context["base_url"] == "https://store.example"
    assert response.context["version"] == __version__
    assert response.context["stats"]["manifests"] == 1
    assert response.context["declares"] is False
    assert response.context["max_upload_mb"] == 2
    assert "store.example" in response.text


@pytest.mark.django_db
def test_index_reuses_statistics_for_a_while(client):
    assert client.get("/").context["stats"]["manifests"] == 0
    assert upload(client, iscc_manifest()).status_code == 200
    assert client.get("/").context["stats"]["manifests"] == 0  # cached for STATISTICS_TTL seconds
    cache.clear()
    assert client.get("/").context["stats"]["manifests"] == 1


@pytest.mark.django_db
def test_index_declaring_store(client, hub):
    response = client.get("/")
    assert response.status_code == 200
    assert response.context["declares"] is True
    assert response.context["hub_url"] == "https://hub.example"
    assert response.context["controller"] == "did:web:store.example"


@pytest.mark.django_db
def test_index_head(client):
    assert client.head("/").status_code == 200


def test_index_refuses_post(client):
    response = client.post("/")
    assert response.status_code == 405
    assert set(response["Allow"].split(", ")) == {"GET", "HEAD"}


def test_docs_references_openapi_json(client):
    response = client.get("/docs")
    assert response.status_code == 200
    assert "/openapi/openapi.json" in response.text


@pytest.mark.parametrize(
    ("name", "media_type"),
    [
        ("openapi.yaml", "application/yaml"),
        ("openapi.json", "application/json"),
        ("c2pa-sbr.json", "application/json"),
    ],
)
def test_openapi_files(client, name, media_type):
    response = client.get(f"/openapi/{name}")
    assert response.status_code == 200
    assert response["Content-Type"] == media_type
    assert response["Cache-Control"] == "no-cache"
    assert response.content == (OPENAPI_DIR / name).read_bytes()
    assert response["Access-Control-Allow-Origin"] == "*"  # the contract is public, like the API


def test_openapi_files_parse(client):
    assert yaml.safe_load(client.get("/openapi/openapi.yaml").content)["openapi"] == "3.1.1"
    assert json.loads(client.get("/openapi/openapi.json").content)["openapi"] == "3.1.1"


@pytest.mark.parametrize("name", ["settings.py", "openapi.yml", "..", "%2e%2e%2fsettings.py", "OPENAPI.JSON"])
def test_openapi_other_files_not_found(client, name):
    assert client.get(f"/openapi/{name}").status_code == 404


def test_openapi_refuses_post(client):
    assert client.post("/openapi/openapi.json").status_code == 405


@pytest.mark.django_db
def test_healthz(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": __version__}


def test_handler404_is_not_found():
    assert urls.handler404 == "iscc_c2pa_store.views.not_found"


@pytest.mark.parametrize("path", ["/v1/unknown", "/v1/manifests/", "/.well-known/unknown", "/.well-known/"])
def test_not_found_under_api_is_json(client, path):
    response = client.get(path)
    assert response.status_code == 404
    assert response["Content-Type"] == "application/json"
    assert response.json() == {"detail": "Not found"}


@pytest.mark.parametrize("path", ["/unknown", "/v1", "/docs/", "/.well-known", "/healthz/x"])
def test_not_found_elsewhere_is_django_page(client, path):
    response = client.get(path)
    assert response.status_code == 404
    assert response["Content-Type"].startswith("text/html")
    assert b"Not Found" in response.content


def test_not_found_view_directly():
    factory = RequestFactory()
    api_response = views.not_found(factory.get("/v1/x"), Exception("x"))
    assert (api_response.status_code, json.loads(api_response.content)) == (404, {"detail": "Not found"})
    page = views.not_found(factory.get("/x"), Exception("x"))
    assert page.status_code == 404
    assert page["Content-Type"].startswith("text/html")
