"""Property-based API tests: Schemathesis generates requests from openapi.yaml and checks every response against it.

Requests go in-process to the Django WSGI application and hit the test database. This catches drift between the
hand-written spec and the code: undocumented status codes, responses that violate their schema (the verbatim C2PA
schemas among them), server errors, and broken authentication. Explicit cases with real Manifest Stores make sure
the success responses of uploads and deletes are validated too, which random bytes alone never reach.
"""

import hashlib

import pytest
import schemathesis
from conftest import iscc_manifest, sign_manifest
from django.core.cache import cache
from django.core.wsgi import get_wsgi_application
from django.test import override_settings
from hypothesis import HealthCheck, settings
from schemathesis.specs.openapi.checks import positive_data_acceptance
from schemathesis.transport.serialization import serialize_binary

from iscc_c2pa_store.views import OPENAPI_DIR

pytestmark = pytest.mark.django_db

TOKEN = "schemathesis-operator-token"  # noqa: S105 (a test credential)
AUTH = {"Authorization": f"Bearer {TOKEN}"}
BASE_URL = "https://store.example"  # an allowed host of the test settings
# A schema-valid string is not necessarily a valid ISCC-SEQ, soft binding value or C2PA Manifest Store, which JSON
# Schema cannot express. The store rightly answers such values with 400, so the check that every schema-valid
# request succeeds does not apply to these operations.
NOT_EXPRESSIBLE = {"POST /v1/manifests", "GET /v1/matches/byBinding", "POST /v1/matches/byBinding"}


@schemathesis.serializer("application/c2pa")
def c2pa_body(ctx, value):
    # type: (object, object) -> bytes
    """Send generated Manifest Store bodies as raw bytes."""
    return serialize_binary(value)


# Loaded from disk so that the relative references to c2pa-sbr.json resolve; requests go to the app in-process.
schema = schemathesis.openapi.from_path(OPENAPI_DIR / "openapi.yaml")
schema.app = get_wsgi_application()


@pytest.fixture(autouse=True, scope="module")
def operator_token():
    """Configure an operator token, so that generated deletes get past authentication when they carry it."""
    with override_settings(ISCC_C2PA_STORE_OPERATOR_TOKEN_SHA256=hashlib.sha256(TOKEN.encode()).hexdigest()):
        yield


def excluded_checks(case):
    # type: (schemathesis.Case) -> list
    """The checks that do not apply to an operation."""
    return [positive_data_acceptance] if case.operation.label in NOT_EXPRESSIBLE else []


def assert_reached_store(case, response):
    # type: (schemathesis.Case, object) -> None
    """Make sure valid generated requests are not all refused before the code under test runs.

    Schema-valid Manifest Store bodies must reach c2pa-rs (a 400 with a validation code, not a 415 for a lost media
    type), and schema-valid deletes must get past authentication.
    """
    if not (case.meta.generation.mode.is_positive and case.method == case.operation.method.upper()):
        return  # negative cases and unsupported-method probes are refused earlier by design
    if case.operation.label == "POST /v1/manifests":
        assert response.status_code == 400
        assert response.json()["code"]
    if case.operation.label == "DELETE /v1/manifests/{activeManifestId}":
        assert response.status_code == 404


@schema.parametrize()
@settings(
    max_examples=40,
    deadline=None,
    derandomize=True,
    database=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)
def test_api_conforms_to_spec(case):
    cache.clear()  # rate limit counters: the fuzzer sends more requests than the test limits allow
    response = case.call_and_validate(BASE_URL, headers=AUTH, excluded_checks=excluded_checks(case))
    assert_reached_store(case, response)


def call(path, method, **kwargs):
    # type: (str, str, object) -> object
    """Send one explicit case of an operation and validate its response against the spec."""
    case = schema[path][method].Case(**kwargs)
    return case.call_and_validate(BASE_URL, headers=AUTH)


def test_upload_conforms():
    response = call("/v1/manifests", "POST", body=iscc_manifest(), media_type="application/c2pa")
    assert response.status_code == 200
    assert response.json()["declaration"] == {"status": "disabled"}


def test_declared_upload_and_did_document_conform(hub):
    response = call("/v1/manifests", "POST", body=iscc_manifest(), media_type="application/c2pa")
    assert response.json()["declaration"]["status"] == "declared"
    assert call("/.well-known/did.json", "GET").status_code == 200


def test_invalid_upload_conforms():
    response = call("/v1/manifests", "POST", body=b"\x00not c2pa", media_type="application/c2pa")
    assert response.status_code == 400
    assert response.json()["code"] == "manifest.unreadable"


def test_fetch_query_and_delete_conform():
    data = sign_manifest([("com.digimarc.validate.1", "AAAA")])
    manifest_id = call("/v1/manifests", "POST", body=data, media_type="application/c2pa").json()["manifestId"]
    fetched = call("/v1/manifests/{activeManifestId}", "GET", path_parameters={"activeManifestId": manifest_id})
    assert fetched.content == data
    matches = call("/v1/matches/byBinding", "GET", query={"alg": "com.digimarc.validate.1", "value": "AAAA"}).json()
    assert matches == {"matches": [{"manifestId": manifest_id, "similarityScore": 100}]}
    deleted = call("/v1/manifests/{activeManifestId}", "DELETE", path_parameters={"activeManifestId": manifest_id})
    assert deleted.status_code == 204
    gone = call("/v1/manifests/{activeManifestId}", "GET", path_parameters={"activeManifestId": manifest_id})
    assert gone.status_code == 410
    conflict = call("/v1/manifests", "POST", body=data, media_type="application/c2pa")
    assert conflict.status_code == 409


def test_delete_without_token_conforms():
    case = schema["/v1/manifests/{activeManifestId}"]["DELETE"].Case(path_parameters={"activeManifestId": "x"})
    response = case.call_and_validate(BASE_URL)
    assert response.status_code == 401


def test_well_known_conforms():
    body = call("/.well-known/c2pa-soft-binding-resolution", "GET").json()
    assert body["capabilitiesEndpoint"] == f"{BASE_URL}/v1/services/capabilities"
    assert body["statusEndpoint"] == f"{BASE_URL}/v1/services/status"
