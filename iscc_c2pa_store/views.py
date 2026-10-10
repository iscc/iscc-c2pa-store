"""Pages for people and the published API contract: landing page, API reference, OpenAPI files, health check."""

from pathlib import Path

from django.conf import settings
from django.db import connection
from django.http import Http404, HttpRequest, HttpResponse, JsonResponse  # noqa: F401 (type comments)
from django.shortcuts import render
from django.views.decorators.http import require_safe
from django.views.defaults import page_not_found

from iscc_c2pa_store import __version__, store

OPENAPI_DIR = Path(__file__).parent / "openapi"
API_PREFIXES = ("/v1/", "/.well-known/")
OPENAPI_FILES = {
    "openapi.yaml": "application/yaml",
    "openapi.json": "application/json",
    "c2pa-sbr.json": "application/json",
}


@require_safe
def index(request):
    # type: (HttpRequest) -> HttpResponse
    """The landing page: what this store is, how to publish and find manifests, and its numbers (cached for 30 s)."""
    context = {
        "version": __version__,
        "base_url": settings.ISCC_C2PA_STORE_URL,
        "host": settings.ISCC_C2PA_STORE_HOST,
        "declares": store.declares(),
        "hub_url": settings.ISCC_C2PA_STORE_HUB_URL,
        "controller": settings.ISCC_C2PA_STORE_CONTROLLER,
        "max_upload_mb": settings.ISCC_C2PA_STORE_MAX_UPLOAD / 1024 / 1024,
        "stats": store.cached_statistics(),
    }
    return render(request, "iscc_c2pa_store/index.html", context)


@require_safe
def docs(request):
    # type: (HttpRequest) -> HttpResponse
    """The API reference, rendered by Stoplight Elements from the hand-written OpenAPI document."""
    return render(request, "iscc_c2pa_store/docs.html", {"version": __version__})


@require_safe
def openapi(request, name):
    # type: (HttpRequest, str) -> HttpResponse
    """Serve the API contract files: our OpenAPI document as YAML and JSON, and the vendored C2PA subset."""
    if name not in OPENAPI_FILES:
        raise Http404(name)
    response = HttpResponse((OPENAPI_DIR / name).read_bytes(), content_type=OPENAPI_FILES[name])
    response["Cache-Control"] = "no-cache"
    return response


def not_found(request, exception):
    # type: (HttpRequest, Exception) -> HttpResponse
    """404 for unknown addresses: the JSON `Error` shape under the API paths, Django's page elsewhere."""
    if request.path.startswith(API_PREFIXES):
        return JsonResponse({"detail": "Not found"}, status=404)
    return page_not_found(request, exception)


@require_safe
def healthz(request):
    # type: (HttpRequest) -> JsonResponse
    """Liveness and readiness: the database answers."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
    return JsonResponse({"status": "ok", "version": __version__})
