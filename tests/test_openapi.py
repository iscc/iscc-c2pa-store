"""Tests that the hand-written spec is valid, matches the routes the store serves, and stays conformant with the C2PA
OpenAPI subset.

`c2pa-sbr.json` is the verbatim subset of the C2PA Soft Binding Resolution API that `scripts/sync_c2pa_openapi.py`
copies from the specification repository. Every operation in it must exist in `openapi.yaml` with the same
operationId, every upstream parameter must be accepted the same way, and every upstream success schema must be used
unchanged, except for the documented extensions listed here.
"""

import json

import pytest
import yaml
from django.urls import URLPattern, URLResolver, get_resolver
from openapi_spec_validator import validate

from iscc_c2pa_store import api
from iscc_c2pa_store.views import OPENAPI_DIR

OURS = yaml.safe_load((OPENAPI_DIR / "openapi.yaml").read_text(encoding="utf-8"))
UPSTREAM = json.loads((OPENAPI_DIR / "c2pa-sbr.json").read_text(encoding="utf-8"))
UPSTREAM_REF = "c2pa-sbr.json#/components/schemas/"
HTTP_METHODS = {"get", "put", "post", "delete", "options", "head", "patch", "trace"}
API_PREFIXES = ("/v1/", "/.well-known/")

# Documented extensions of upstream success schemas: the upstream schema name and the schema of ours that extends it.
EXTENDED_RESULTS = {
    "c2pa.softBindingQueryResult": "#/components/schemas/QueryResult",  # matches carry `isccId`
    "c2pa.manifestCreateResult": "#/components/schemas/StoreResult",  # adds `manifestUrl` and `declaration`
}

UPSTREAM_OPERATIONS = [
    (path, method) for path, item in UPSTREAM["paths"].items() for method in item if method in HTTP_METHODS
]


def our_path(path):
    # type: (str) -> str
    """Map an upstream path (relative to the /v1 server URL) to the store's absolute path."""
    return path if path.startswith("/.well-known/") else f"/v1{path}"


def resolve(spec, item):
    # type: (dict, dict) -> dict
    """Follow a local `$ref` such as `#/components/responses/X`."""
    if not item.get("$ref", "").startswith("#/"):
        return item
    node = spec
    for part in item["$ref"].removeprefix("#/").split("/"):
        node = node[part]
    return node


def json_schema_ref(response):
    # type: (dict) -> str | None
    """The `$ref` of a response's application/json schema, if any."""
    return response.get("content", {}).get("application/json", {}).get("schema", {}).get("$ref")


def success_statuses(operation):
    # type: (dict) -> set[str]
    """The 2xx statuses an operation documents."""
    return {status for status in operation["responses"] if status.startswith("2")}


def url_patterns(patterns, prefix=""):
    # type: (list, str) -> list[str]
    """The routes of a Django URL configuration as absolute paths, with `{name}` for converters."""
    found = []
    for pattern in patterns:
        route = prefix + str(pattern.pattern)
        if isinstance(pattern, URLResolver):
            found.extend(url_patterns(pattern.url_patterns, route))
        elif isinstance(pattern, URLPattern):
            found.append("/" + route.replace("<", "{").replace(">", "}"))
    return found


def routed_operations():
    # type: () -> set[tuple[str, str]]
    """The (path, method) pairs django-ninja serves; HEAD is left out because GET implies it (RFC 9110)."""
    return {
        (prefix + path, method)
        for prefix, router in api.api._routers
        for path, path_view in router.path_operations.items()
        for operation in path_view.operations
        for method in operation.methods
        if method != "HEAD"
    }


def test_json_spec_is_current():
    assert json.loads((OPENAPI_DIR / "openapi.json").read_text(encoding="utf-8")) == OURS


@pytest.mark.parametrize("name", ["openapi.yaml", "openapi.json", "c2pa-sbr.json"])
def test_spec_files_are_valid(name):
    path = OPENAPI_DIR / name
    spec = yaml.safe_load(path.read_text(encoding="utf-8"))
    validate(spec, base_uri=path.as_uri())


def test_upstream_subset_is_attributed():
    assert UPSTREAM["info"]["license"]["name"] == "Creative Commons Attribution 4.0 International"
    assert UPSTREAM["info"]["license"]["url"] == "https://creativecommons.org/licenses/by/4.0/"
    assert UPSTREAM["info"]["x-source"].startswith("https://github.com/c2pa-org/specs-core/blob/")
    assert "c2pa-sbr.json" in OURS["info"]["description"]
    assert "CC BY 4.0" in OURS["info"]["description"]


def test_spec_implements_c2pa_specification_version():
    assert UPSTREAM["info"]["version"] == api.SPEC_VERSION


def test_our_version_is_package_version():
    from iscc_c2pa_store import __version__

    assert OURS["info"]["version"] == __version__


def test_every_upstream_operation_is_covered():
    # A guard against an empty parametrization when c2pa-sbr.json changes shape.
    assert len(UPSTREAM_OPERATIONS) == 9


@pytest.mark.parametrize(("path", "method"), UPSTREAM_OPERATIONS)
def test_operation_is_implemented_with_upstream_operation_id(path, method):
    upstream = UPSTREAM["paths"][path][method]
    ours = OURS["paths"][our_path(path)][method]
    assert ours["operationId"] == upstream["operationId"]


@pytest.mark.parametrize(("path", "method"), UPSTREAM_OPERATIONS)
def test_upstream_parameters_are_accepted(path, method):
    ours = OURS["paths"][our_path(path)][method].get("parameters", [])
    ours_by_key = {(p["name"], p["in"]): p for p in (resolve(OURS, p) for p in ours)}
    for parameter in UPSTREAM["paths"][path][method].get("parameters", []):
        mine = ours_by_key[(parameter["name"], parameter["in"])]
        assert mine.get("required", False) == parameter.get("required", False), parameter["name"]
        assert mine["schema"]["type"] == parameter["schema"]["type"], parameter["name"]


@pytest.mark.parametrize(("path", "method"), UPSTREAM_OPERATIONS)
def test_upstream_request_body_is_accepted(path, method):
    upstream = UPSTREAM["paths"][path][method].get("requestBody")
    ours = OURS["paths"][our_path(path)][method].get("requestBody")
    if upstream is None:
        assert ours is None
        return
    assert ours["required"] == upstream["required"]
    assert set(ours["content"]) == set(upstream["content"])


@pytest.mark.parametrize(("path", "method"), UPSTREAM_OPERATIONS)
def test_upstream_success_schema_is_used_unchanged(path, method):
    upstream_operation = UPSTREAM["paths"][path][method]
    our_operation = OURS["paths"][our_path(path)][method]
    assert success_statuses(our_operation) == success_statuses(upstream_operation)
    for status in success_statuses(upstream_operation):
        upstream = upstream_operation["responses"][status]
        ours = resolve(OURS, our_operation["responses"][status])
        upstream_ref = json_schema_ref(upstream)
        if upstream_ref is None:  # not JSON: the application/c2pa Manifest Store, or no content
            assert ours.get("content", {}) == upstream["content"]
            continue
        name = upstream_ref.rsplit("/", 1)[1]
        assert json_schema_ref(ours) == EXTENDED_RESULTS.get(name, UPSTREAM_REF + name)


def test_query_body_is_upstream_schema():
    body = OURS["paths"]["/v1/matches/byBinding"]["post"]["requestBody"]["content"]["application/json"]["schema"]
    assert body["$ref"] == UPSTREAM_REF + "c2pa.softBindingQuery"


def test_store_result_extends_upstream_create_result():
    parts = OURS["components"]["schemas"]["StoreResult"]["allOf"]
    assert parts[0]["$ref"] == UPSTREAM_REF + "c2pa.manifestCreateResultIdOnly"
    upstream = UPSTREAM["components"]["schemas"]["c2pa.manifestCreateResult"]["allOf"]
    assert upstream[0]["$ref"] == "#/components/schemas/c2pa.manifestCreateResultIdOnly"
    assert set(parts[1]["properties"]).isdisjoint(upstream[1]["properties"])  # no clash with the upstream receipt


def test_match_extends_upstream_match():
    parts = OURS["components"]["schemas"]["Match"]["allOf"]
    assert parts[0]["$ref"] == UPSTREAM_REF + "c2pa.softBindingQueryResult/properties/matches/items"
    query_result = OURS["components"]["schemas"]["QueryResult"]
    assert query_result["properties"]["matches"]["items"]["$ref"] == "#/components/schemas/Match"


def test_extensions_do_not_redefine_upstream_fields():
    upstream_match = UPSTREAM["components"]["schemas"]["c2pa.softBindingQueryResult"]["properties"]["matches"]
    upstream_fields = upstream_match["items"]["properties"]
    ours = OURS["components"]["schemas"]["Match"]["allOf"][1]["properties"]
    for name in set(ours) & set(upstream_fields):
        assert ours[name]["type"] == upstream_fields[name]["type"], name


def test_spec_paths_match_api_routes():
    documented = {(path, method.upper()) for path, item in OURS["paths"].items() for method in item}
    assert documented == routed_operations()


def test_spec_paths_match_django_urls():
    served = {path for path in url_patterns(get_resolver().url_patterns) if path.startswith(API_PREFIXES)}
    assert served == set(OURS["paths"])


def get_routes():
    # type: () -> list[set[str]]
    """The method sets of the django-ninja operations that answer GET."""
    return [
        set(operation.methods)
        for _prefix, router in api.api._routers
        for path_view in router.path_operations.values()
        for operation in path_view.operations
        if "GET" in operation.methods
    ]


def test_every_get_route_registers_head():
    methods = get_routes()
    assert len(methods) == len([item for item in OURS["paths"].values() if "get" in item])
    assert all("HEAD" in m for m in methods)


GET_PATHS = sorted(path for path, item in OURS["paths"].items() if "get" in item and path != api.MANIFEST_PATH)


@pytest.mark.django_db
@pytest.mark.parametrize("path", GET_PATHS)
def test_every_get_route_answers_head(client, hub, path):
    response = client.head(path, {"alg": "com.digimarc.validate.1", "value": "AAAA"})
    assert response.status_code == 200
    assert response.content == b""
