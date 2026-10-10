"""HTTP API: the store, fetch, query and service routes of the C2PA Soft Binding Resolution API.

The contract is the hand-written `openapi/openapi.yaml`; request and response models in `schema/` are generated from
it. django-ninja routes and validates, its own OpenAPI generation and docs are switched off so that the served spec
is the hand-written one. Every error is JSON `{"detail": ...}` with the status the C2PA OpenAPI documents: invalid
parameters are 400 (never 422), unknown manifests 404.
"""

import hashlib
import hmac
import json
from pathlib import Path
from typing import Annotated

from django.conf import settings
from django.http import HttpRequest, HttpResponse  # noqa: F401 (type comments)
from django.utils.http import parse_etags
from ninja import NinjaAPI
from ninja.errors import AuthenticationError, HttpError, ValidationError
from ninja.params.functions import Query
from ninja.security import HttpBearer
from ninja.throttling import BaseThrottle

from iscc_c2pa_store import __version__, binding, manifest, store
from iscc_c2pa_store.schema import Declaration as DeclarationResult
from iscc_c2pa_store.schema import DidDocument, Match, QueryResult, StoreResult
from iscc_c2pa_store.schema.c2pa import (
    ServiceCapabilities,
    ServiceStatus,
    SoftBindingAlgList,
    SoftBindingQuery,
    WellKnownDiscovery,
)
from iscc_c2pa_store.throttle import WindowThrottle

C2PA_OPENAPI = Path(__file__).parent / "openapi" / "c2pa-sbr.json"
# The C2PA specification requires the reported version to equal `info.version` of the implemented API definition
SPEC_VERSION = json.loads(C2PA_OPENAPI.read_text(encoding="utf-8"))["info"]["version"]
CAPABILITIES = ["storeManifests"]
MANIFEST_PATH = "/v1/manifests/{activeManifestId}"
GET = ["GET", "HEAD"]  # HEAD answers wherever GET does (RFC 9110)
NOT_FOUND = "C2PA Manifest not found"

api = NinjaAPI(
    title="ISCC C2PA Store",
    version=__version__,
    docs_url=None,
    openapi_url=None,
    urls_namespace="api",
)
read_limit = [WindowThrottle("read", "ISCC_C2PA_STORE_RATE_READ")]  # type: list[BaseThrottle]
write_limit = [WindowThrottle("write", "ISCC_C2PA_STORE_RATE_WRITE")]  # type: list[BaseThrottle]
MaxResults = Annotated[int, Query(ge=1)]


class OperatorToken(HttpBearer):
    """Bearer authentication with the operator token, compared by SHA-256 digest in constant time."""

    def authenticate(self, request, token):
        # type: (HttpRequest, str) -> bool
        """Accept the token whose SHA-256 hex digest is configured; refuse all when none is configured."""
        expected = settings.ISCC_C2PA_STORE_OPERATOR_TOKEN_SHA256
        digest = hashlib.sha256(token.encode()).hexdigest()
        return bool(expected) and hmac.compare_digest(digest, expected)


def error(request, status, detail, code=None):
    # type: (HttpRequest, int, str, str | None) -> HttpResponse
    """A JSON error response in the shape of the `Error` schema."""
    body = {"detail": detail} | ({"code": code} if code else {})
    return api.create_response(request, body, status=status)


@api.exception_handler(ValidationError)
def invalid_request(request, exc):
    # type: (HttpRequest, ValidationError) -> HttpResponse
    """Answer invalid parameters and bodies with 400, as the C2PA OpenAPI documents, naming the first problem."""
    first = exc.errors[0] if exc.errors else {}
    where = ".".join(str(part) for part in first.get("loc", ()))
    return error(request, 400, f"{where}: {first.get('msg', 'invalid')}" if where else "Invalid request")


@api.exception_handler(AuthenticationError)
def unauthorized(request, exc):
    # type: (HttpRequest, AuthenticationError) -> HttpResponse
    """Answer missing or wrong operator tokens with 401 and a bearer challenge."""
    response = error(request, 401, "Operator token required")
    response["WWW-Authenticate"] = "Bearer"
    return response


@api.exception_handler(HttpError)
def http_error(request, exc):
    # type: (HttpRequest, HttpError) -> HttpResponse
    """Answer HTTP errors, rate limits among them, in the `Error` shape."""
    return error(request, exc.status_code, str(exc))


def client_address(request):
    # type: (HttpRequest) -> str
    """The client address, from X-Forwarded-For behind the configured number of trusted proxies."""
    return BaseThrottle().get_ident(request) or ""


def store_result(record):
    # type: (store.Manifest) -> StoreResult
    """The upload result of a stored manifest: its ID, its address and where its declaration stands."""
    declaration = record.declaration
    return StoreResult(
        manifestId=record.manifest_id,
        manifestUrl=store.manifest_url(record.manifest_id),
        declaration=DeclarationResult(
            status=declaration.status,  # type: ignore[arg-type]  # the model's enum has the same values
            isccId=declaration.iscc_id or None,
            hub=declaration.hub or None,
        ),
    )


@api.post("/v1/manifests", response=StoreResult, exclude_none=True, throttle=write_limit, tags=["store"])
def add_manifest(request, returnReceipt: bool = False, declare: bool | None = None):
    """Store a C2PA Manifest Store sent as `application/c2pa`, and declare its ISCC soft binding."""
    if request.content_type != manifest.MEDIA_TYPE:
        return error(request, 415, f"Send the C2PA Manifest Store as {manifest.MEDIA_TYPE}")
    declare_it = settings.ISCC_C2PA_STORE_DECLARE_DEFAULT if declare is None else declare
    try:
        record = store.ingest(request.body, declare_it, store.uploader_key(client_address(request)))
    except manifest.InvalidManifestError as e:
        return error(request, 400, e.message, e.code)
    except store.ConflictError as e:
        return error(request, 409, str(e))
    return store_result(record)


def etag_matches(header, etag):
    # type: (str, str) -> bool
    """Tell whether an If-None-Match header matches an entity tag, by weak comparison (RFC 9110)."""
    tags = parse_etags(header)
    return "*" in tags or etag in (tag.removeprefix("W/") for tag in tags)


@api.api_operation(GET, MANIFEST_PATH, throttle=read_limit, tags=["fetch"])
def get_manifest(request, activeManifestId: str, returnActiveManifest: bool = False):
    """Return a stored C2PA Manifest Store byte for byte."""
    try:
        record = store.fetch(activeManifestId)
        etag = f'"{record.sha256}"'
        fresh = etag_matches(request.headers.get("If-None-Match", ""), etag)
        body = b"" if fresh else store.read_bytes(record)
    except store.NotFoundError:
        return error(request, 404, NOT_FOUND)
    except store.GoneError:
        return error(request, 410, "C2PA Manifest deleted")
    response = HttpResponse(status=304) if fresh else HttpResponse(body, content_type=manifest.MEDIA_TYPE)
    response["ETag"] = etag
    response["Cache-Control"] = "public, max-age=300"
    return response


@api.delete(MANIFEST_PATH, auth=OperatorToken(), tags=["store"])
def delete_manifest(request, activeManifestId: str):
    """Delete a stored C2PA Manifest Store and withdraw its declaration."""
    try:
        store.delete(activeManifestId)
    except store.NotFoundError:
        return error(request, 404, NOT_FOUND)
    except store.GoneError:
        return error(request, 410, "C2PA Manifest deleted")
    return HttpResponse(status=204)


def matches(request, alg, value, max_results):
    # type: (HttpRequest, str, str, int) -> QueryResult | HttpResponse
    """Run a soft binding query; invalid algorithms and values are 400."""
    try:
        found = store.query(alg, binding.decode_base64(value), max_results)
    except ValueError as e:
        return error(request, 400, str(e))
    items = [Match(manifestId=m.manifest_id, similarityScore=m.score, isccId=m.iscc_id or None) for m in found]
    return QueryResult(matches=items)


@api.api_operation(
    GET, "/v1/matches/byBinding", response=QueryResult, exclude_none=True, throttle=read_limit, tags=["query"]
)
def query_by_binding(request, alg: str, value: str, maxResults: MaxResults = 10):
    """Find stored manifests by soft binding."""
    return matches(request, alg, value, maxResults)


@api.post("/v1/matches/byBinding", response=QueryResult, exclude_none=True, throttle=read_limit, tags=["query"])
def query_by_large_binding(request, payload: SoftBindingQuery, maxResults: MaxResults = 10):
    """Find stored manifests by a soft binding sent as JSON."""
    return matches(request, payload.alg, payload.value, maxResults)


@api.api_operation(
    GET, "/v1/services/supportedAlgorithms", response=SoftBindingAlgList, exclude_none=True, tags=["service"]
)
def supported_algorithms(request):
    # type: (HttpRequest) -> dict
    """The soft binding algorithms this repository answers queries for."""
    kinds = store.supported_algorithms()
    return {
        "fingerprints": [{"alg": alg} for alg in kinds["fingerprint"]],
        "watermarks": [{"alg": alg} for alg in kinds["watermark"]],
    }


@api.api_operation(GET, "/v1/services/status", response=ServiceStatus, tags=["service"])
def service_status(request):
    # type: (HttpRequest) -> dict
    """The operational status of the store."""
    return {"status": store.service_status(), "timestamp": store.timestamp()}


@api.api_operation(GET, "/v1/services/capabilities", response=ServiceCapabilities, tags=["service"])
def service_capabilities(request):
    # type: (HttpRequest) -> dict
    """The implemented C2PA specification version and the optional capabilities of this repository."""
    return {"c2paSpecificationVersion": SPEC_VERSION, "supportedCapabilities": CAPABILITIES}


@api.api_operation(
    GET, "/.well-known/c2pa-soft-binding-resolution", response=WellKnownDiscovery, exclude_none=True, tags=["service"]
)
def well_known(request):
    # type: (HttpRequest) -> dict
    """Discovery information: where the versioned API, its capabilities and its status live, and the spec version."""
    base = f"{settings.ISCC_C2PA_STORE_URL}/v1"
    return {
        "apiEndpoint": base,
        "c2paSpecificationVersion": SPEC_VERSION,
        "capabilitiesEndpoint": f"{base}/services/capabilities",
        "statusEndpoint": f"{base}/services/status",
    }


@api.api_operation(GET, "/.well-known/did.json", response=DidDocument, tags=["identity"])
def did_document(request):
    """The did:web document with the public key that signs this store's ISCC declarations."""
    key = store.keypair()
    if key is None:
        return error(request, 404, "This store does not declare")
    return HttpResponse(json.dumps(key.controller_document), content_type="application/json")
