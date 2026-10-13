"""HTTP tests of every route and status that `openapi/openapi.yaml` documents, with real Manifest Stores.

Uploads are signed by `conftest.sign_manifest`, declarations go to the fake hub, and every error is checked for the
`{"detail"}` shape and the C2PA status (400, never 422).
"""

import base64
import hashlib
import pathlib
import re
from datetime import timedelta
from urllib.parse import quote

import iscc_crypto as icr
import pytest
from conftest import (
    C2PA_RS_MANIFEST,
    DATA_CODE,
    HUB_URL,
    INSTANCE,
    META_64,
    VECTOR_1,
    encode,
    flip,
    iscc_manifest,
    iscc_units,
    sign_manifest,
    upload,
)
from django.test import RequestFactory

from iscc_c2pa_store import api, store
from iscc_c2pa_store.models import Declaration, Manifest

TOKEN = "operator-test-token"  # noqa: S105 (a test credential)
ISCC_ID = re.compile(r"^ISCC:[A-Z2-7]{16}$")
WATERMARK = "com.digimarc.validate.1"  # on the C2PA Soft Binding Algorithm List
QUERY = "/v1/matches/byBinding"


@pytest.fixture
def operator(settings):
    # type: (object) -> dict
    """Configure the operator token and return the request headers that carry it."""
    settings.ISCC_C2PA_STORE_OPERATOR_TOKEN_SHA256 = hashlib.sha256(TOKEN.encode()).hexdigest()
    return {"Authorization": f"Bearer {TOKEN}"}


def path(manifest_id):
    # type: (str) -> str
    """The percent-encoded manifest address path of a manifest ID."""
    return f"/v1/manifests/{quote(manifest_id, safe='')}"


def stored(client, data, **query):
    # type: (object, bytes, object) -> dict
    """Upload a Manifest Store that must be accepted and return the upload result."""
    response = upload(client, data, **query)
    assert response.status_code == 200, response.content
    return response.json()


def assert_error(response, status):
    # type: (object, int) -> dict
    """Check a JSON error response in the `Error` shape and return its body."""
    assert response.status_code == status, response.content
    assert response["Content-Type"] == "application/json; charset=utf-8"
    body = response.json()
    assert isinstance(body["detail"], str)
    assert body["detail"]
    assert set(body) <= {"detail", "code"}
    return body


def query(client, alg, value, **params):
    # type: (object, str, str, object) -> object
    """GET a soft binding query."""
    return client.get(QUERY, {"alg": alg, "value": value, **params})


def watermark(value):
    # type: (bytes) -> str
    """Base64 text of a watermark binding value."""
    return base64.b64encode(value).decode("ascii")


# POST /v1/manifests


@pytest.mark.django_db
def test_upload_result_without_declarations(client):
    data = iscc_manifest()
    body = stored(client, data)
    manifest_id = body["manifestId"]
    assert manifest_id.startswith("urn:c2pa:")
    assert body == {
        "manifestId": manifest_id,
        "manifestUrl": f"https://store.example/v1/manifests/{manifest_id}",
        "declaration": {"status": "disabled"},
    }
    assert "%" not in body["manifestUrl"]
    assert client.get(body["manifestUrl"].removeprefix("https://store.example")).content == data
    assert Manifest.objects.get(pk=manifest_id).sha256 == hashlib.sha256(data).hexdigest()


@pytest.mark.django_db
def test_upload_declares(client, hub):
    body = stored(client, iscc_manifest())
    declaration = body["declaration"]
    assert declaration["status"] == "declared"
    assert ISCC_ID.match(declaration["isccId"])
    assert declaration["hub"] == HUB_URL
    assert list(hub.declared) == [declaration["isccId"]]
    assert hub.declared[declaration["isccId"]]["gateway"] == body["manifestUrl"]


@pytest.mark.django_db
def test_upload_declare_false(client, hub):
    body = stored(client, iscc_manifest(), declare=False)
    assert body["declaration"] == {"status": "skipped"}
    assert hub.requests == []


@pytest.mark.django_db
def test_upload_declare_true_overrides_setting(client, hub, settings):
    settings.ISCC_C2PA_STORE_DECLARE_DEFAULT = False
    assert stored(client, iscc_manifest(), declare=True)["declaration"]["status"] == "declared"


@pytest.mark.django_db
@pytest.mark.parametrize(("default", "status"), [(True, "declared"), (False, "skipped")])
def test_upload_declare_default_from_settings(client, hub, settings, default, status):
    settings.ISCC_C2PA_STORE_DECLARE_DEFAULT = default
    assert stored(client, iscc_manifest())["declaration"]["status"] == status


@pytest.mark.django_db
def test_upload_hub_down_is_pending(client, hub):
    hub.mode = "down"
    body = stored(client, iscc_manifest())
    assert body["declaration"] == {"status": "pending", "hub": HUB_URL}


@pytest.mark.django_db
def test_upload_hub_refusal_is_failed(client, hub):
    hub.mode = "refuse"
    assert stored(client, iscc_manifest())["declaration"] == {"status": "failed", "hub": HUB_URL}


@pytest.mark.django_db
def test_upload_without_iscc_binding_is_skipped(client, hub):
    assert stored(client, sign_manifest())["declaration"] == {"status": "skipped"}


@pytest.mark.django_db
def test_upload_return_receipt_is_ignored(client):
    data = iscc_manifest()
    body = stored(client, data, returnReceipt=True)
    assert set(body) == {"manifestId", "manifestUrl", "declaration"}
    assert stored(client, data, returnReceipt=False) == body


@pytest.mark.django_db
def test_upload_is_idempotent(client, hub):
    data = iscc_manifest()
    first = stored(client, data)
    assert stored(client, data) == first
    assert len(hub.declared) == 1


@pytest.mark.django_db
def test_upload_c2pa_rs_fixture(client):
    body = stored(client, C2PA_RS_MANIFEST)
    assert client.get(path(body["manifestId"])).content == C2PA_RS_MANIFEST


@pytest.mark.django_db
def test_upload_invalid_manifest_is_400_with_code(client):
    body = assert_error(upload(client, b"not a manifest store"), 400)
    assert body["code"] == "manifest.unreadable"


@pytest.mark.django_db
def test_upload_empty_body_is_400(client):
    response = client.generic("POST", "/v1/manifests", b"", CONTENT_TYPE="application/c2pa")
    assert assert_error(response, 400)["code"] == "manifest.unreadable"


@pytest.mark.django_db
def test_upload_tampered_manifest_is_400(client):
    data = bytearray(iscc_manifest())
    data[len(data) // 2] ^= 0xFF
    body = assert_error(upload(client, bytes(data)), 400)
    assert body["code"]


@pytest.mark.django_db
def test_upload_with_bytes_after_the_manifest_store_is_400(client):
    body = assert_error(upload(client, C2PA_RS_MANIFEST + b"<html>anything</html>"), 400)
    assert body["code"] == "manifest.trailingData"
    assert not Manifest.objects.exists()


@pytest.mark.django_db
def test_upload_with_too_many_iscc_bindings_is_400(client):
    bindings = [("io.iscc.v0", encode(iscc_units(f"block {i}".encode() * 100))) for i in range(9)]
    body = assert_error(upload(client, sign_manifest(bindings)), 400)
    assert body["code"] == "manifest.tooManyBindings"
    assert not Manifest.objects.exists()


@pytest.mark.django_db
def test_upload_with_too_many_iscc_units_is_400(client):
    units = [flip(DATA_CODE, bits) for bits in range(1, 34)]  # 33 distinct Data-Codes
    body = assert_error(upload(client, iscc_manifest(units)), 400)
    assert body["code"] == "manifest.tooManyBindings"


@pytest.mark.django_db
def test_upload_deleted_manifest_is_409(client, operator):
    data = iscc_manifest()
    manifest_id = stored(client, data)["manifestId"]
    assert client.delete(path(manifest_id), headers=operator).status_code == 204
    body = assert_error(upload(client, data), 409)
    assert manifest_id in body["detail"]


@pytest.mark.django_db
@pytest.mark.parametrize("content_type", ["application/octet-stream", "application/json", "text/plain"])
def test_upload_wrong_media_type_is_415(client, content_type):
    response = client.post("/v1/manifests", data=iscc_manifest(), content_type=content_type)
    assert assert_error(response, 415)["detail"] == "Send the C2PA Manifest Store as application/c2pa"
    assert not Manifest.objects.exists()


@pytest.mark.django_db
def test_upload_media_type_parameters_are_accepted(client):
    data = sign_manifest()
    response = client.generic("POST", "/v1/manifests", data, content_type="application/c2pa; charset=binary")
    assert response.status_code == 200


def test_upload_too_large_is_413(client, settings):
    response = upload(client, b"\0" * (settings.ISCC_C2PA_STORE_MAX_UPLOAD + 1))
    assert response.status_code == 413
    assert response.json() == {"detail": f"Request body exceeds {settings.ISCC_C2PA_STORE_MAX_UPLOAD} bytes"}


@pytest.mark.django_db
@pytest.mark.parametrize("parameter", ["declare=maybe", "returnReceipt=2"])
def test_upload_invalid_parameter_is_400(client, parameter):
    response = client.post(f"/v1/manifests?{parameter}", data=sign_manifest(), content_type="application/c2pa")
    assert_error(response, 400)


# Rate limits use a day window in these tests, so a minute boundary during a test cannot reset the count.


def upload_from(client, address, data=b"not a manifest store"):
    # type: (object, str, bytes) -> object
    """POST a Manifest Store from a client address behind one trusted proxy (requires NINJA_NUM_PROXIES = 1)."""
    return client.post(
        "/v1/manifests", data=data, content_type="application/c2pa", headers={"X-Forwarded-For": address}
    )


@pytest.mark.django_db
def test_upload_rate_limit_is_429(client, settings):
    settings.ISCC_C2PA_STORE_RATE_WRITE = "1/d"
    stored(client, sign_manifest())
    response = upload(client, sign_manifest())
    body = assert_error(response, 429)
    assert body["detail"]
    assert 0 < int(response["Retry-After"]) <= 86400
    assert response["Access-Control-Expose-Headers"] == "ETag, Retry-After"


@pytest.mark.django_db
def test_refused_uploads_count_against_the_limit(client, settings):
    settings.ISCC_C2PA_STORE_RATE_WRITE = "1/d"
    assert_error(upload(client, b"not a manifest store"), 400)
    assert_error(upload(client, sign_manifest()), 429)


@pytest.mark.django_db
def test_rate_limit_is_per_client_address(client, settings):
    settings.NINJA_NUM_PROXIES = 1
    settings.ISCC_C2PA_STORE_RATE_WRITE = "1/d"
    assert upload_from(client, "203.0.113.1").status_code == 400
    assert upload_from(client, "203.0.113.2").status_code == 400
    assert upload_from(client, "203.0.113.1").status_code == 429


@pytest.mark.django_db
def test_rate_limit_counts_an_ipv6_64_as_one_client(client, settings):
    settings.NINJA_NUM_PROXIES = 1
    settings.ISCC_C2PA_STORE_RATE_WRITE = "1/d"
    assert upload_from(client, "2001:db8:1:2::1").status_code == 400
    assert upload_from(client, "2001:db8:1:2:a:b:c:d").status_code == 429  # same /64, another address
    assert upload_from(client, "2001:db8:1:3::1").status_code == 400  # another /64


@pytest.mark.django_db
def test_unthrottled_clients_are_not_limited(client, settings):
    settings.NINJA_NUM_PROXIES = 1
    settings.ISCC_C2PA_STORE_RATE_READ = "1/d"
    settings.ISCC_C2PA_STORE_UNTHROTTLED = ["203.0.113.9"]
    resolver = {"X-Forwarded-For": "203.0.113.9"}
    other = {"X-Forwarded-For": "203.0.113.1"}
    for _ in range(3):
        assert client.get("/v1/manifests/urn%3Ac2pa%3Aunknown", headers=resolver).status_code == 404
    assert client.get("/v1/manifests/urn%3Ac2pa%3Aunknown", headers=other).status_code == 404
    assert client.get("/v1/manifests/urn%3Ac2pa%3Aunknown", headers=other).status_code == 429


@pytest.mark.django_db
def test_reads_do_not_use_up_the_upload_limit(client, settings):
    settings.ISCC_C2PA_STORE_RATE_WRITE = "2/d"
    for _ in range(2):
        assert query(client, WATERMARK, "AAAA").status_code == 200
    assert upload(client, sign_manifest()).status_code == 200


@pytest.mark.django_db
def test_uploads_do_not_use_up_the_read_limit(client, settings):
    settings.ISCC_C2PA_STORE_RATE_READ = "1/d"
    for _ in range(2):
        stored(client, sign_manifest())
    assert query(client, WATERMARK, "AAAA").status_code == 200
    assert query(client, WATERMARK, "AAAA").status_code == 429


@pytest.mark.django_db
def test_read_rate_limit_is_429(client, settings):
    settings.ISCC_C2PA_STORE_RATE_READ = "1/d"
    assert query(client, WATERMARK, "AAAA").status_code == 200
    response = client.get("/v1/manifests/urn%3Ac2pa%3Aunknown")
    assert_error(response, 429)
    assert 0 < int(response["Retry-After"]) <= 86400


@pytest.mark.django_db
def test_rate_limits_follow_settings(client, settings):
    settings.ISCC_C2PA_STORE_RATE_READ = "1/d"
    assert query(client, WATERMARK, "AAAA").status_code == 200
    assert query(client, WATERMARK, "AAAA").status_code == 429
    settings.ISCC_C2PA_STORE_RATE_READ = "3/d"
    assert query(client, WATERMARK, "AAAA").status_code == 200


# X-Forwarded-For


def test_client_address_ignores_forwarded_for_without_proxies(settings):
    settings.NINJA_NUM_PROXIES = 0
    request = RequestFactory().get("/", REMOTE_ADDR="10.0.0.1", HTTP_X_FORWARDED_FOR="203.0.113.9")
    assert api.client_address(request) == "10.0.0.1"


def test_client_address_behind_one_proxy(settings):
    settings.NINJA_NUM_PROXIES = 1
    request = RequestFactory().get("/", REMOTE_ADDR="10.0.0.1", HTTP_X_FORWARDED_FOR="198.51.100.66, 203.0.113.9")
    assert api.client_address(request) == "203.0.113.9"  # the address the trusted proxy saw, not a spoofed one


def test_client_address_behind_proxy_without_header(settings):
    settings.NINJA_NUM_PROXIES = 1
    assert api.client_address(RequestFactory().get("/", REMOTE_ADDR="10.0.0.1")) == "10.0.0.1"


def test_client_address_missing():
    request = RequestFactory().get("/")
    del request.META["REMOTE_ADDR"]
    assert api.client_address(request) == ""


@pytest.mark.django_db
@pytest.mark.parametrize(("proxies", "address"), [(0, "127.0.0.1"), (1, "203.0.113.9")])
def test_uploader_is_keyed_client_address(client, settings, proxies, address):
    settings.NINJA_NUM_PROXIES = proxies
    response = client.post(
        "/v1/manifests",
        data=sign_manifest(),
        content_type="application/c2pa",
        headers={"X-Forwarded-For": "198.51.100.66, 203.0.113.9"},
    )
    record = Manifest.objects.get(pk=response.json()["manifestId"])
    assert record.uploader == store.uploader_key(address)
    assert address not in record.uploader


# GET and HEAD /v1/manifests/{activeManifestId}


@pytest.mark.django_db
def test_get_manifest_byte_identical(client):
    data = iscc_manifest()
    body = stored(client, data)
    response = client.get(path(body["manifestId"]))
    assert response.status_code == 200
    assert response.content == data
    assert response["Content-Type"] == "application/c2pa"
    assert response["ETag"] == f'"{hashlib.sha256(data).hexdigest()}"'
    assert response["Cache-Control"] == "public, max-age=300"
    assert response["Access-Control-Allow-Origin"] == "*"


@pytest.mark.django_db
def test_get_manifest_by_manifest_url(client):
    data = iscc_manifest()
    body = stored(client, data)
    assert client.get(body["manifestUrl"].removeprefix("https://store.example")).content == data


@pytest.mark.django_db
def test_get_manifest_unencoded_and_lowercase_encoded(client):
    data = sign_manifest()
    manifest_id = stored(client, data)["manifestId"]
    assert client.get(f"/v1/manifests/{manifest_id}").content == data
    assert client.get(f"/v1/manifests/{manifest_id.replace(':', '%3a')}").content == data


@pytest.mark.django_db
def test_get_manifest_return_active_manifest_is_ignored(client):
    data = sign_manifest()
    manifest_id = stored(client, data)["manifestId"]
    assert client.get(path(manifest_id), {"returnActiveManifest": "true"}).content == data


@pytest.mark.django_db
def test_get_manifest_invalid_parameter_is_400(client):
    manifest_id = stored(client, sign_manifest())["manifestId"]
    assert_error(client.get(path(manifest_id), {"returnActiveManifest": "perhaps"}), 400)


@pytest.mark.django_db
def test_head_manifest(client):
    data = sign_manifest()
    manifest_id = stored(client, data)["manifestId"]
    response = client.head(path(manifest_id))
    assert response.status_code == 200
    assert response.content == b""
    assert response["Content-Type"] == "application/c2pa"
    assert response["ETag"] == f'"{hashlib.sha256(data).hexdigest()}"'


@pytest.mark.django_db
def test_get_manifest_not_modified(client):
    data = sign_manifest()
    manifest_id = stored(client, data)["manifestId"]
    etag = f'"{hashlib.sha256(data).hexdigest()}"'
    response = client.get(path(manifest_id), headers={"If-None-Match": etag})
    assert response.status_code == 304
    assert response.content == b""
    assert response["ETag"] == etag
    assert response["Cache-Control"] == "public, max-age=300"


@pytest.mark.django_db
@pytest.mark.parametrize("etag", ['"0000"', "W/0000", ""])
def test_get_manifest_other_etag_is_200(client, etag):
    data = sign_manifest()
    manifest_id = stored(client, data)["manifestId"]
    response = client.get(path(manifest_id), headers={"If-None-Match": etag})
    assert response.status_code == 200
    assert response.content == data


@pytest.mark.django_db
@pytest.mark.parametrize("header", ['"0000", {etag}', "W/{etag}", "*", '{etag},"0000"'])
def test_get_manifest_not_modified_for_etag_lists(client, header):
    data = sign_manifest()
    manifest_id = stored(client, data)["manifestId"]
    etag = f'"{hashlib.sha256(data).hexdigest()}"'
    assert client.get(path(manifest_id), headers={"If-None-Match": header.format(etag=etag)}).status_code == 304


@pytest.mark.parametrize(
    ("header", "matched"),
    [
        ('"abc"', True),
        ('W/"abc"', True),
        ('"0000", "abc"', True),
        ('"0000",W/"abc"', True),
        ("*", True),
        ('"0000"', False),
        ('W/"0000", "abcd"', False),
        ("abc", False),
        ("", False),
    ],
)
def test_etag_matches_by_weak_comparison(header, matched):
    assert api.etag_matches(header, '"abc"') is matched


@pytest.mark.django_db
@pytest.mark.parametrize("method", ["get", "head"])
def test_get_unknown_manifest_is_404(client, method):
    response = getattr(client, method)("/v1/manifests/urn%3Ac2pa%3A00000000-0000-0000-0000-000000000000")
    assert response.status_code == 404
    if method == "get":
        assert assert_error(response, 404) == {"detail": "C2PA Manifest not found"}


@pytest.mark.django_db
@pytest.mark.parametrize("method", ["get", "head", "delete"])
@pytest.mark.parametrize(
    "manifest_id", ["urn:c2pa:" + "a" * 247, "urn:c2pa:line\nbreak", "urn:c2pa:\x00", "urn:c2pa:tab\tx"]
)
def test_impossible_manifest_id_is_404(client, operator, method, manifest_id):
    response = getattr(client, method)(path(manifest_id), headers=operator)
    assert response.status_code == 404
    if method != "head":
        assert assert_error(response, 404) == {"detail": "C2PA Manifest not found"}


@pytest.mark.django_db
def test_longest_possible_manifest_id_is_looked_up(client):
    manifest_id = "urn:c2pa:" + "a" * 246
    assert len(manifest_id) == 255
    assert assert_error(client.get(path(manifest_id)), 404) == {"detail": "C2PA Manifest not found"}


@pytest.mark.django_db
def test_get_deleted_manifest_is_410(client, operator):
    manifest_id = stored(client, sign_manifest())["manifestId"]
    assert client.delete(path(manifest_id), headers=operator).status_code == 204
    assert assert_error(client.get(path(manifest_id)), 410) == {"detail": "C2PA Manifest deleted"}
    assert client.head(path(manifest_id)).status_code == 410


@pytest.mark.django_db
@pytest.mark.parametrize("method", ["get", "head"])
def test_get_manifest_with_missing_file_is_404(client, caplog, method):
    # A restore from a backup can leave a live record without its file; the client gets 404, the operator a log.
    manifest_id = stored(client, sign_manifest())["manifestId"]
    record = Manifest.objects.get(pk=manifest_id)
    pathlib.Path(record.blob.path).unlink()
    response = getattr(client, method)(path(manifest_id))
    assert response.status_code == 404
    if method == "get":
        assert assert_error(response, 404) == {"detail": "C2PA Manifest not found"}
    assert any(r.levelname == "ERROR" and manifest_id in r.getMessage() for r in caplog.records)


@pytest.mark.django_db
def test_get_manifest_with_missing_file_is_not_modified_for_its_etag(client):
    # A conditional request that matches needs no bytes, so it does not notice the missing file.
    data = sign_manifest()
    manifest_id = stored(client, data)["manifestId"]
    pathlib.Path(Manifest.objects.get(pk=manifest_id).blob.path).unlink()
    etag = f'"{hashlib.sha256(data).hexdigest()}"'
    assert client.get(path(manifest_id), headers={"If-None-Match": etag}).status_code == 304


@pytest.mark.django_db
def test_upload_after_release_is_accepted(client, operator):
    data = sign_manifest()
    manifest_id = stored(client, data)["manifestId"]
    assert client.delete(path(manifest_id), headers=operator).status_code == 204
    assert_error(upload(client, data), 409)
    Declaration.objects.update(updated_at=store.timestamp() - store.LEASE - timedelta(seconds=1))  # nothing in flight
    assert store.release(manifest_id) is True
    assert_error(client.get(path(manifest_id)), 404)
    assert stored(client, data)["manifestId"] == manifest_id
    assert client.get(path(manifest_id)).content == data


# DELETE /v1/manifests/{activeManifestId}


@pytest.mark.django_db
def test_delete_manifest(client, operator):
    value = watermark(b"delete me")
    manifest_id = stored(client, sign_manifest([(WATERMARK, value)]))["manifestId"]
    response = client.delete(path(manifest_id), headers=operator)
    assert response.status_code == 204
    assert response.content == b""
    assert query(client, WATERMARK, value).json() == {"matches": []}
    assert Manifest.objects.get(pk=manifest_id).deleted_at is not None


@pytest.mark.django_db
def test_delete_withdraws_declaration(client, hub, operator):
    body = stored(client, iscc_manifest())
    assert len(hub.declared) == 1
    assert client.delete(path(body["manifestId"]), headers=operator).status_code == 204
    assert hub.declared == {}
    assert hub.requests[-1].method == "DELETE"


@pytest.mark.django_db
def test_delete_twice_is_410(client, operator):
    manifest_id = stored(client, sign_manifest())["manifestId"]
    assert client.delete(path(manifest_id), headers=operator).status_code == 204
    assert assert_error(client.delete(path(manifest_id), headers=operator), 410) == {"detail": "C2PA Manifest deleted"}


@pytest.mark.django_db
def test_delete_unknown_is_404(client, operator):
    response = client.delete("/v1/manifests/urn%3Ac2pa%3Aunknown", headers=operator)
    assert assert_error(response, 404) == {"detail": "C2PA Manifest not found"}


@pytest.mark.django_db
@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer wrong-token"},
        {"Authorization": f"Basic {TOKEN}"},
        {"Authorization": "Bearer"},
        {"Authorization": f"Bearer {TOKEN}x"},
    ],
)
def test_delete_without_valid_token_is_401(client, operator, headers):
    manifest_id = stored(client, sign_manifest())["manifestId"]
    response = client.delete(path(manifest_id), headers=headers)
    assert assert_error(response, 401) == {"detail": "Operator token required"}
    assert response["WWW-Authenticate"] == "Bearer"
    assert client.get(path(manifest_id)).status_code == 200


@pytest.mark.django_db
def test_delete_unknown_without_token_is_401(client, operator):
    # Authentication comes first, so unauthenticated callers cannot probe which manifests exist.
    assert_error(client.delete("/v1/manifests/urn%3Ac2pa%3Aunknown"), 401)


@pytest.mark.django_db
@pytest.mark.parametrize("token", [TOKEN, ""])
def test_delete_without_configured_token_is_401(client, settings, token):
    settings.ISCC_C2PA_STORE_OPERATOR_TOKEN_SHA256 = ""
    manifest_id = stored(client, sign_manifest())["manifestId"]
    response = client.delete(path(manifest_id), headers={"Authorization": f"Bearer {token}"})
    assert_error(response, 401)
    assert response["WWW-Authenticate"] == "Bearer"


@pytest.mark.django_db
def test_delete_with_digest_as_token_is_401(client, operator, settings):
    manifest_id = stored(client, sign_manifest())["manifestId"]
    digest = settings.ISCC_C2PA_STORE_OPERATOR_TOKEN_SHA256
    assert_error(client.delete(path(manifest_id), headers={"Authorization": f"Bearer {digest}"}), 401)


# GET and POST /v1/matches/byBinding


@pytest.mark.django_db
def test_query_exact_match(client):
    value = watermark(b"\x01\x02\x03watermark")
    manifest_id = stored(client, sign_manifest([(WATERMARK, value)]))["manifestId"]
    stored(client, sign_manifest([(WATERMARK, watermark(b"other"))]))
    response = query(client, WATERMARK, value)
    assert response.status_code == 200
    assert response.json() == {"matches": [{"manifestId": manifest_id, "similarityScore": 100}]}


@pytest.mark.django_db
def test_query_exact_match_lenient_base64(client):
    raw = b"len\xfb\xff\xfe-lenient"
    value = watermark(raw)
    assert "+" in value.strip("+")
    manifest_id = stored(client, sign_manifest([(WATERMARK, value)]))["manifestId"]
    urlsafe = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    assert query(client, WATERMARK, urlsafe).json()["matches"][0]["manifestId"] == manifest_id
    # A "+" sent without percent-encoding arrives as a space.
    unencoded = client.get(f"{QUERY}?alg={WATERMARK}&value={value}")
    assert unencoded.json()["matches"][0]["manifestId"] == manifest_id


@pytest.mark.django_db
def test_query_unencoded_leading_plus(client):
    value = watermark(b"\xfb\xff\xfe-plus first")
    assert value.startswith("+")
    manifest_id = stored(client, sign_manifest([(WATERMARK, value)]))["manifestId"]
    unencoded = client.get(f"{QUERY}?alg={WATERMARK}&value={value}")
    assert unencoded.json()["matches"] == [{"manifestId": manifest_id, "similarityScore": 100}]


@pytest.mark.django_db
def test_query_surrounding_spaces_are_plus_signs(client):
    value = watermark(b"spaced")
    manifest_id = stored(client, sign_manifest([(WATERMARK, value)]))["manifestId"]
    assert query(client, WATERMARK, f"\t{value}\r\n").json()["matches"][0]["manifestId"] == manifest_id
    # A space is an unencoded "+", so " value " is the different value "+value+".
    assert query(client, WATERMARK, f" {value} ").json() == {"matches": []}


@pytest.mark.django_db
def test_query_no_match(client):
    assert query(client, WATERMARK, watermark(b"nothing")).json() == {"matches": []}


@pytest.mark.django_db
def test_query_iscc_similarity(client):
    units = iscc_units(b"similar content" * 100)
    manifest_id = stored(client, iscc_manifest(units))["manifestId"]
    data_code = units[0]
    near = query(client, "io.iscc.v0", encode([flip(data_code, 26)])).json()["matches"]
    assert near == [{"manifestId": manifest_id, "similarityScore": 90}]
    equal_data = query(client, "io.iscc.v0", encode([data_code])).json()["matches"]
    assert equal_data == [{"manifestId": manifest_id, "similarityScore": 99}]
    equal_instance = query(client, "io.iscc.v0", encode([flip(data_code, 200), units[1]])).json()["matches"]
    assert equal_instance == [{"manifestId": manifest_id, "similarityScore": 100}]
    far = query(client, "io.iscc.v0", encode([flip(data_code, 128)])).json()
    assert far == {"matches": []}


@pytest.mark.django_db
def test_query_iscc_iep_vector(client):
    manifest_id = stored(client, iscc_manifest([DATA_CODE, INSTANCE]))["manifestId"]
    matches = query(client, "io.iscc.v0", VECTOR_1).json()["matches"]
    assert matches == [{"manifestId": manifest_id, "similarityScore": 100}]


@pytest.mark.django_db
def test_query_match_carries_iscc_id(client, hub):
    units = iscc_units(b"declared content" * 100)
    body = stored(client, iscc_manifest(units))
    matches = query(client, "io.iscc.v0", encode(units)).json()["matches"]
    assert matches == [
        {"manifestId": body["manifestId"], "similarityScore": 100, "isccId": body["declaration"]["isccId"]}
    ]


@pytest.mark.django_db
def test_query_max_results(client):
    value = watermark(b"shared watermark")
    ids = [stored(client, sign_manifest([(WATERMARK, value)]))["manifestId"] for _ in range(3)]
    assert [m["manifestId"] for m in query(client, WATERMARK, value).json()["matches"]] == ids  # earliest first
    assert len(query(client, WATERMARK, value, maxResults=2).json()["matches"]) == 2
    assert len(query(client, WATERMARK, value, maxResults=10**6).json()["matches"]) == 3


@pytest.mark.django_db
def test_query_ranks_trusted_signers_first(client, settings, trust_anchors):
    value = watermark(b"claimed by two")
    untrusted = stored(client, sign_manifest([(WATERMARK, value)]))["manifestId"]
    settings.ISCC_C2PA_STORE_TRUST_ANCHORS = trust_anchors
    trusted = stored(client, sign_manifest([(WATERMARK, value)]))["manifestId"]
    matches = query(client, WATERMARK, value).json()["matches"]
    assert [m["manifestId"] for m in matches] == [trusted, untrusted]  # despite the later upload
    assert set(matches[0]) == {"manifestId", "similarityScore"}  # trust is not part of the response


@pytest.mark.django_db
def test_query_with_most_iscc_units_is_answered(client):
    units = [flip(DATA_CODE, bits) for bits in range(1, store.MAX_UNITS + 1)]
    assert query(client, "io.iscc.v0", encode(units)).json() == {"matches": []}


@pytest.mark.django_db
@pytest.mark.parametrize("method", ["get", "post"])
def test_query_with_too_many_iscc_units_is_400(client, method):
    units = [flip(DATA_CODE, bits) for bits in range(1, store.MAX_UNITS + 2)]
    params = {"alg": "io.iscc.v0", "value": encode(units)}
    if method == "get":
        response = client.get(QUERY, params)
    else:
        response = client.post(QUERY, params, content_type="application/json")
    assert assert_error(response, 400) == {"detail": f"Value holds more than {store.MAX_UNITS} searchable ISCC units"}


@pytest.mark.django_db
@pytest.mark.parametrize(
    "params",
    [
        {"alg": WATERMARK, "value": "AAAA", "maxResults": "0"},
        {"alg": WATERMARK, "value": "AAAA", "maxResults": "-1"},
        {"alg": WATERMARK, "value": "AAAA", "maxResults": "ten"},
        {"alg": WATERMARK, "value": "AAAA", "maxResults": "1.5"},
        {"alg": WATERMARK},
        {"value": "AAAA"},
        {},
        {"alg": WATERMARK, "value": "!!!not base64"},
        {"alg": WATERMARK, "value": "A" * 4097},
        {"alg": "com.example.unknown", "value": "AAAA"},
        {"alg": "", "value": "AAAA"},
        {"alg": "io.iscc.v0", "value": "AAAA"},
        {"alg": "io.iscc.v0", "value": encode([META_64])},
    ],
)
def test_query_invalid_is_400(client, params):
    assert_error(client.get(QUERY, params), 400)


@pytest.mark.django_db
def test_query_error_names_problem(client):
    assert assert_error(query(client, "com.example.unknown", "AAAA"), 400) == {
        "detail": "Unknown soft binding algorithm: com.example.unknown"
    }
    assert "maxResults" in assert_error(query(client, WATERMARK, "AAAA", maxResults=0), 400)["detail"]


@pytest.mark.django_db
def test_post_query(client):
    value = watermark(b"posted watermark")
    manifest_id = stored(client, sign_manifest([(WATERMARK, value)]))["manifestId"]
    response = client.post(QUERY, {"alg": WATERMARK, "value": value}, content_type="application/json")
    assert response.status_code == 200
    assert response.json() == {"matches": [{"manifestId": manifest_id, "similarityScore": 100}]}


@pytest.mark.django_db
def test_post_query_iscc_and_max_results(client):
    units = iscc_units(b"posted content" * 100)
    for _ in range(2):
        stored(client, iscc_manifest(units))
    body = {"alg": "io.iscc.v0", "value": encode(units)}
    assert len(client.post(QUERY, body, content_type="application/json").json()["matches"]) == 2
    assert len(client.post(f"{QUERY}?maxResults=1", body, content_type="application/json").json()["matches"]) == 1
    assert_error(client.post(f"{QUERY}?maxResults=0", body, content_type="application/json"), 400)


@pytest.mark.django_db
@pytest.mark.parametrize(
    "body",
    [
        b"{not json",
        b"",
        b"[]",
        b'{"alg": "io.iscc.v0"}',
        b'{"value": "AAAA"}',
        b'{"alg": 1, "value": "AAAA"}',
        b'{"alg": "com.example.unknown", "value": "AAAA"}',
        b'{"alg": "io.iscc.v0", "value": "@@@@"}',
    ],
)
def test_post_query_invalid_is_400(client, body):
    assert_error(client.post(QUERY, body, content_type="application/json"), 400)


@pytest.mark.django_db
def test_query_results_are_cors_readable(client):
    response = query(client, WATERMARK, "AAAA", HTTP_ORIGIN="https://other.example")
    assert response["Access-Control-Allow-Origin"] == "*"


# Service routes


@pytest.mark.django_db
def test_supported_algorithms(client):
    assert client.get("/v1/services/supportedAlgorithms").json() == {
        "fingerprints": [{"alg": "io.iscc.v0"}],
        "watermarks": [],
    }
    stored(client, sign_manifest([(WATERMARK, watermark(b"indexed")), ("com.example.unlisted", "AAAA")]))
    assert client.get("/v1/services/supportedAlgorithms").json() == {
        "fingerprints": [{"alg": "io.iscc.v0"}],
        "watermarks": [{"alg": WATERMARK}],
    }


@pytest.mark.django_db
def test_service_status(client):
    response = client.get("/v1/services/status")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert re.match(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d", body["timestamp"])


def test_service_capabilities(client):
    assert client.get("/v1/services/capabilities").json() == {
        "c2paSpecificationVersion": api.SPEC_VERSION,
        "supportedCapabilities": ["storeManifests"],
    }


def test_well_known_discovery(client):
    response = client.get("/.well-known/c2pa-soft-binding-resolution")
    assert response.status_code == 200
    body = response.json()
    assert body == {
        "apiEndpoint": "https://store.example/v1",
        "c2paSpecificationVersion": api.SPEC_VERSION,
        "capabilitiesEndpoint": "https://store.example/v1/services/capabilities",
        "statusEndpoint": "https://store.example/v1/services/status",
    }
    assert api.SPEC_VERSION == "2.4.0"
    assert response["Access-Control-Allow-Origin"] == "*"


@pytest.mark.django_db
@pytest.mark.parametrize("field", ["capabilitiesEndpoint", "statusEndpoint"])
def test_well_known_endpoints_are_served(client, field):
    url = client.get("/.well-known/c2pa-soft-binding-resolution").json()[field]
    assert client.get(url.removeprefix("https://store.example")).status_code == 200


def test_well_known_discovery_has_no_null_fields(client):
    body = client.get("/.well-known/c2pa-soft-binding-resolution").json()
    assert None not in body.values()


def test_did_document_without_key_is_404(client):
    assert assert_error(client.get("/.well-known/did.json"), 404) == {"detail": "This store does not declare"}


def test_did_document_without_hub_is_404(client, settings, hub):
    settings.ISCC_C2PA_STORE_HUB_URL = ""
    assert_error(client.get("/.well-known/did.json"), 404)


@pytest.mark.django_db
def test_did_document_verifies_store_notes(client, hub):
    response = client.get("/.well-known/did.json")
    assert response.status_code == 200
    assert response["Content-Type"] == "application/json"
    document = response.json()
    assert document["id"] == "did:web:store.example"
    method = document["verificationMethod"][0]
    assert method["controller"] == "did:web:store.example"
    assert method["id"].startswith("did:web:store.example#")
    stored(client, iscc_manifest())
    (note,) = hub.declared.values()
    assert note["signature"]["pubkey"] == method["publicKeyMultibase"]
    result = icr.verify_json(note, identity_doc=document)
    assert result.signature_valid
    assert result.identity_verified


# Methods, CORS and the routes outside the API


@pytest.mark.parametrize(
    ("method", "url", "allowed"),
    [
        ("put", "/v1/manifests", {"POST"}),
        ("get", "/v1/manifests", {"POST"}),
        ("post", "/v1/manifests/urn%3Ac2pa%3Ax", {"GET", "HEAD", "DELETE"}),
        ("patch", "/v1/manifests/urn%3Ac2pa%3Ax", {"GET", "HEAD", "DELETE"}),
        ("delete", QUERY, {"GET", "HEAD", "POST"}),
        ("post", "/v1/services/status", {"GET", "HEAD"}),
        ("delete", "/.well-known/did.json", {"GET", "HEAD"}),
    ],
)
def test_wrong_method_is_405(client, method, url, allowed):
    response = getattr(client, method)(url)
    assert response.status_code == 405
    assert set(response["Allow"].split(", ")) == allowed


def test_preflight_on_api(client):
    response = client.options(
        "/v1/manifests",
        headers={"Origin": "https://other.example", "Access-Control-Request-Method": "POST"},
    )
    assert response.status_code == 204
    assert response["Access-Control-Allow-Origin"] == "*"
    assert response["Access-Control-Allow-Methods"] == "GET, HEAD, POST, OPTIONS"


@pytest.mark.django_db
def test_no_cors_on_admin(client):
    response = client.get("/admin/login/", headers={"Origin": "https://other.example"})
    assert response.status_code == 200
    assert "Access-Control-Allow-Origin" not in response
    preflight = client.options(
        "/admin/login/", headers={"Origin": "https://other.example", "Access-Control-Request-Method": "POST"}
    )
    assert "Access-Control-Allow-Origin" not in preflight


@pytest.mark.parametrize("url", ["/v1/unknown", "/v1/manifests/", "/.well-known/unknown"])
def test_unknown_api_path_is_404(client, url):
    response = client.get(url)
    assert response.status_code == 404
    assert response.json() == {"detail": "Not found"}
    assert response["Access-Control-Allow-Origin"] == "*"
