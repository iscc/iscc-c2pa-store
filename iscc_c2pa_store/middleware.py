"""Request guards that run before any view: request body limits, cross-origin access to the public API, and the
rate limit of operator logins.
"""

import math

from django.conf import settings
from django.http import HttpRequest, HttpResponse, JsonResponse  # noqa: F401 (type comments)

from iscc_c2pa_store.throttle import WindowThrottle

UPLOAD_PATH = "/v1/manifests"
OTHER_BODY_LIMIT = 64 * 1024  # bytes; queries, admin forms
PUBLIC_PREFIXES = ("/v1/", "/.well-known/", "/openapi/")
CORS_METHODS = "GET, HEAD, POST, OPTIONS"  # never DELETE: operator calls do not come from browsers of other sites
LOGIN_PATH = "/admin/login/"
login_limit = WindowThrottle("login", "ISCC_C2PA_STORE_RATE_LOGIN")


class UploadLimitMiddleware:
    """Refuse request bodies above the limit of their route with 413 before anything reads them."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # type: (HttpRequest) -> HttpResponse
        """Check the declared Content-Length against the limit of the route."""
        limit = settings.ISCC_C2PA_STORE_MAX_UPLOAD if request.path == UPLOAD_PATH else OTHER_BODY_LIMIT
        length = request.META.get("CONTENT_LENGTH") or "0"
        if not (length.isascii() and length.isdigit()):
            return JsonResponse({"detail": "Invalid Content-Length"}, status=400)
        if int(length) > limit:
            return JsonResponse({"detail": f"Request body exceeds {limit} bytes"}, status=413)
        return self.get_response(request)


class PublicCorsMiddleware:
    """Let web pages of any origin use the public API without credentials, answering preflights directly."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # type: (HttpRequest) -> HttpResponse
        """Add CORS headers to public API responses; answer preflight requests with 204."""
        if not request.path.startswith(PUBLIC_PREFIXES):
            return self.get_response(request)
        is_preflight = request.method == "OPTIONS" and "HTTP_ACCESS_CONTROL_REQUEST_METHOD" in request.META
        response = HttpResponse(status=204) if is_preflight else self.get_response(request)
        response["Access-Control-Allow-Origin"] = "*"
        response["Access-Control-Expose-Headers"] = "ETag, Retry-After"
        if is_preflight:
            response["Access-Control-Allow-Methods"] = CORS_METHODS
            response["Access-Control-Allow-Headers"] = "Content-Type, If-None-Match"
            response["Access-Control-Max-Age"] = "86400"
        return response


class LoginThrottleMiddleware:
    """Limit operator login attempts per client, so passwords cannot be guessed at speed."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # type: (HttpRequest) -> HttpResponse
        """Answer 429 with Retry-After to login attempts above the limit."""
        if request.method == "POST" and request.path == LOGIN_PATH and not login_limit.allow_request(request):
            response = HttpResponse("Too many login attempts", status=429, content_type="text/plain")
            response["Retry-After"] = str(math.ceil(login_limit.wait()))
            return response
        return self.get_response(request)
