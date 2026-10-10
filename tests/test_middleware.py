"""Tests of the request guards: body limits per route (413), CORS for the public API and the operator login limit.

Login rates use a day window, so a minute boundary during a test cannot reset the count.
"""

import json

import pytest
from django.http import HttpResponse
from django.test import RequestFactory

from iscc_c2pa_store.middleware import (
    LOGIN_PATH,
    OTHER_BODY_LIMIT,
    LoginThrottleMiddleware,
    PublicCorsMiddleware,
    UploadLimitMiddleware,
)


def ok(request):
    # type: (object) -> HttpResponse
    """A view stand-in that accepts every request."""
    return HttpResponse("passed")


def limit(meta, path="/v1/manifests"):
    # type: (dict, str) -> HttpResponse
    """Run a POST with the given WSGI environ entries through the body limit."""
    return UploadLimitMiddleware(ok)(RequestFactory().post(path, data=b"", content_type="text/plain", **meta))


def test_upload_at_limit_passes(settings):
    response = limit({"CONTENT_LENGTH": str(settings.ISCC_C2PA_STORE_MAX_UPLOAD)})
    assert response.content == b"passed"


def test_upload_above_limit_is_413(settings):
    response = limit({"CONTENT_LENGTH": str(settings.ISCC_C2PA_STORE_MAX_UPLOAD + 1)})
    assert response.status_code == 413
    assert json.loads(response.content) == {
        "detail": f"Request body exceeds {settings.ISCC_C2PA_STORE_MAX_UPLOAD} bytes"
    }


def test_upload_limit_follows_settings(settings):
    settings.ISCC_C2PA_STORE_MAX_UPLOAD = 100
    assert limit({"CONTENT_LENGTH": "100"}).status_code == 200
    assert limit({"CONTENT_LENGTH": "101"}).status_code == 413


def test_other_routes_have_small_limit():
    path = "/v1/matches/byBinding"
    assert limit({"CONTENT_LENGTH": str(OTHER_BODY_LIMIT)}, path).status_code == 200
    response = limit({"CONTENT_LENGTH": str(OTHER_BODY_LIMIT + 1)}, path)
    assert response.status_code == 413
    assert json.loads(response.content) == {"detail": f"Request body exceeds {OTHER_BODY_LIMIT} bytes"}


def test_upload_limit_is_exact_path():
    # Only the upload route gets the upload limit, not paths below it.
    assert limit({"CONTENT_LENGTH": str(OTHER_BODY_LIMIT + 1)}, "/v1/manifests/x").status_code == 413


@pytest.mark.parametrize("length", ["", "0"])
def test_missing_or_zero_length_passes(length):
    assert limit({"CONTENT_LENGTH": length}).status_code == 200


@pytest.mark.parametrize("length", ["-1", "1e3", "abc", " 10", "0x10"])
def test_invalid_length_is_400(length):
    response = limit({"CONTENT_LENGTH": length})
    assert response.status_code == 400
    assert json.loads(response.content) == {"detail": "Invalid Content-Length"}


def test_superscript_length_is_400():
    # WSGI decodes header bytes as Latin-1, so Content-Length: \xb2 arrives as "²".
    response = limit({"CONTENT_LENGTH": "²"})
    assert response.status_code == 400
    assert json.loads(response.content) == {"detail": "Invalid Content-Length"}


def test_client_request_above_upload_limit_is_413(client, settings):
    settings.ISCC_C2PA_STORE_MAX_UPLOAD = 1024
    response = client.post("/v1/manifests", data=b"x" * 1025, content_type="application/c2pa")
    assert response.status_code == 413
    assert response["Content-Type"] == "application/json"
    assert response["Access-Control-Allow-Origin"] == "*"  # a browser on another site can read the refusal


def test_client_invalid_length_is_400_with_cors(client):
    response = client.generic("POST", "/v1/matches/byBinding", b"{}", "application/json", CONTENT_LENGTH="abc")
    assert response.status_code == 400
    assert response.json() == {"detail": "Invalid Content-Length"}
    assert response["Access-Control-Allow-Origin"] == "*"


def test_cors_runs_before_body_limit(settings):
    order = settings.MIDDLEWARE
    cors_index = order.index("iscc_c2pa_store.middleware.PublicCorsMiddleware")
    assert cors_index < order.index("iscc_c2pa_store.middleware.UploadLimitMiddleware")


def test_client_query_above_body_limit_is_413(client):
    body = b'{"alg": "io.iscc.v0", "value": "' + b"A" * OTHER_BODY_LIMIT + b'"}'
    response = client.post("/v1/matches/byBinding", data=body, content_type="application/json")
    assert response.status_code == 413


def cors(method, path, **meta):
    # type: (str, str, object) -> HttpResponse
    """Run a request through the CORS middleware in front of a view that accepts everything."""
    return PublicCorsMiddleware(ok)(RequestFactory().generic(method, path, **meta))


@pytest.mark.parametrize("path", ["/v1/manifests/x", "/.well-known/did.json", "/openapi/openapi.json"])
def test_cors_on_public_paths(path):
    response = cors("GET", path, HTTP_ORIGIN="https://other.example")
    assert response.content == b"passed"
    assert response["Access-Control-Allow-Origin"] == "*"
    assert response["Access-Control-Expose-Headers"] == "ETag, Retry-After"
    assert "Access-Control-Allow-Methods" not in response
    assert "Access-Control-Allow-Credentials" not in response


@pytest.mark.parametrize("path", ["/", "/docs", "/admin/", "/healthz", "/v1", "/static/brand/favicon.svg"])
def test_no_cors_elsewhere(path):
    response = cors("GET", path, HTTP_ORIGIN="https://other.example")
    assert "Access-Control-Allow-Origin" not in response


def test_preflight_is_answered_directly():
    response = cors(
        "OPTIONS",
        "/v1/matches/byBinding",
        HTTP_ORIGIN="https://other.example",
        HTTP_ACCESS_CONTROL_REQUEST_METHOD="POST",
        HTTP_ACCESS_CONTROL_REQUEST_HEADERS="content-type",
    )
    assert response.status_code == 204
    assert response.content == b""
    assert response["Access-Control-Allow-Origin"] == "*"
    assert response["Access-Control-Allow-Methods"] == "GET, HEAD, POST, OPTIONS"
    assert "DELETE" not in response["Access-Control-Allow-Methods"]
    assert response["Access-Control-Allow-Headers"] == "Content-Type, If-None-Match"
    assert response["Access-Control-Max-Age"] == "86400"


def test_options_without_preflight_header_reaches_view():
    response = cors("OPTIONS", "/v1/manifests")
    assert response.content == b"passed"
    assert response["Access-Control-Allow-Origin"] == "*"
    assert "Access-Control-Allow-Methods" not in response


def test_preflight_outside_public_api_is_not_answered():
    response = cors("OPTIONS", "/admin/", HTTP_ACCESS_CONTROL_REQUEST_METHOD="POST")
    assert response.content == b"passed"
    assert "Access-Control-Allow-Origin" not in response


def login(method="post", path=LOGIN_PATH, address="203.0.113.1"):
    # type: (str, str, str) -> HttpResponse
    """Run a request through the login limit in front of a view that accepts everything."""
    return LoginThrottleMiddleware(ok)(getattr(RequestFactory(), method)(path, REMOTE_ADDR=address))


def test_login_attempts_above_limit_are_429(settings):
    settings.ISCC_C2PA_STORE_RATE_LOGIN = "2/d"
    assert [login().content for _ in range(2)] == [b"passed", b"passed"]
    response = login()
    assert response.status_code == 429
    assert response["Content-Type"] == "text/plain"
    assert response.content == b"Too many login attempts"
    assert 0 < int(response["Retry-After"]) <= 86400


def test_login_limit_is_per_client_address(settings):
    settings.ISCC_C2PA_STORE_RATE_LOGIN = "1/d"
    assert login(address="203.0.113.1").status_code == 200
    assert login(address="203.0.113.1").status_code == 429
    assert login(address="203.0.113.2").status_code == 200


def test_login_limit_counts_ipv6_networks(settings):
    settings.ISCC_C2PA_STORE_RATE_LOGIN = "1/d"
    assert login(address="2001:db8:1:2::1").status_code == 200
    assert login(address="2001:db8:1:2::2").status_code == 429


def test_login_limit_spares_unthrottled_addresses(settings):
    settings.ISCC_C2PA_STORE_RATE_LOGIN = "1/d"
    settings.ISCC_C2PA_STORE_UNTHROTTLED = ["203.0.113.9"]
    assert [login(address="203.0.113.9").status_code for _ in range(3)] == [200, 200, 200]


@pytest.mark.parametrize(("method", "path"), [("get", LOGIN_PATH), ("post", "/admin/"), ("post", "/v1/manifests")])
def test_login_limit_ignores_other_requests(settings, method, path):
    settings.ISCC_C2PA_STORE_RATE_LOGIN = "1/d"
    assert [login(method, path).status_code for _ in range(3)] == [200, 200, 200]
    assert login().status_code == 200  # other requests did not use up the login limit


def test_login_limit_follows_settings(settings):
    settings.ISCC_C2PA_STORE_RATE_LOGIN = "1/d"
    assert login().status_code == 200
    assert login().status_code == 429
    settings.ISCC_C2PA_STORE_RATE_LOGIN = "3/d"
    assert login().status_code == 200


def test_login_limit_runs_before_sessions(settings):
    order = settings.MIDDLEWARE
    throttle_index = order.index("iscc_c2pa_store.middleware.LoginThrottleMiddleware")
    assert throttle_index < order.index("django.contrib.sessions.middleware.SessionMiddleware")


@pytest.mark.django_db
def test_client_login_attempts_above_limit_are_429(client, settings):
    settings.ISCC_C2PA_STORE_RATE_LOGIN = "1/d"
    credentials = {"username": "operator", "password": "wrong password"}
    assert client.post(LOGIN_PATH, credentials).status_code == 200  # the form again, with an error
    response = client.post(LOGIN_PATH, credentials)
    assert response.status_code == 429
    assert response["Content-Type"] == "text/plain"
    assert int(response["Retry-After"]) > 0
    assert "Access-Control-Allow-Origin" not in response
    assert client.get(LOGIN_PATH).status_code == 200  # the login page itself stays reachable
