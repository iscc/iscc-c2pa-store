# CLAUDE.md

Guidance for coding agents working in this repository.

## Project

**iscc-c2pa-store** is a C2PA Manifest Repository with a Soft Binding Resolution API, built on Django and
django-ninja. It stores C2PA Manifest Stores (`POST /v1/manifests`), serves them byte for byte at their manifest
address (`GET /v1/manifests/{manifestId}`), finds them by soft binding (`/v1/matches/byBinding`: exact match for
every algorithm on the C2PA Soft Binding Algorithm List, ISCC similarity for `io.iscc.v0`), and declares the
`io.iscc.v0` soft binding of each upload on the ISCC Discovery Protocol under its own did:web key, with the manifest
address as gateway URL. That makes manifests findable network-wide through iscc-c2pa-resolver.

Public instances: `c2pa-store.iscc.io` (mainnet, hub `iscc.id`) and `c2pa-store-test.iscc.io` (testnet, hub
`staging.iscc.id`). Related: `../iscc-c2pa-resolver` (network-wide lookup), `../iscc-c2pa-demo` (desktop app that
publishes to the store), `../iscc-hub` (declarations).

## Commands

```bash
uv sync                     # dev and docs dependencies
uv run prek install         # git hooks (once per clone)
uv run poe all              # format, type check, test (local loop)
uv run poe ci               # lint, type check, test, audit without modifying files (CI also runs prek hooks)
uv run poe test             # pytest, 100% branch coverage required
uv run pytest tests/test_store.py::test_ingest_is_idempotent
uv run poe serve            # dev server on 127.0.0.1:45470 (needs a .env, see .env.example)
uv run poe conformance      # C2PA conformance harness against the dev server (needs Node)
uv run poe codegen          # regenerate openapi.json and schema/ from openapi/openapi.yaml
uv run poe sync-c2pa <dir>  # refresh openapi/c2pa-sbr.json from a C2PA specs-core checkout
uv run poe vendor-c2pa      # refresh the vendored C2PA algorithm list and trust list
uv run poe docs-serve       # docs preview on 127.0.0.1:45471
```

## Layout

- `iscc_c2pa_store/openapi/openapi.yaml` - hand-written API contract (source of truth); `openapi.json` is generated
    from it, `c2pa-sbr.json` is the verbatim C2PA OpenAPI subset (`scripts/sync_c2pa_openapi.py`)
- `iscc_c2pa_store/schema/` - pydantic models generated from `openapi.yaml` (`uv run poe codegen`); never edit
- `iscc_c2pa_store/vendor/` - C2PA Soft Binding Algorithm List and trust list (`scripts/vendor_c2pa.py`); never edit
- `iscc_c2pa_store/api.py` - django-ninja routes: HTTP mapping only, errors as `{"detail"}` with C2PA statuses
- `iscc_c2pa_store/store.py` - service layer: ingest, fetch, delete, query, declarations and retries
- `iscc_c2pa_store/manifest.py` - c2pa-rs reading and the acceptance policy for uploaded Manifest Stores
- `iscc_c2pa_store/binding.py` - base64 and ISCC-SEQ decoding (IEP-0020), searchable units
- `iscc_c2pa_store/similarity.py` - Hamming similarity and the score rule shared with the resolver
- `iscc_c2pa_store/declare.py` - signed IsccNotes and the ISCC hub client
- `iscc_c2pa_store/models.py`, `admin.py` - data model and the unfold operator dashboard (`/admin/`)
- `iscc_c2pa_store/views.py`, `templates/`, `static/` - landing page, Stoplight API reference (`/docs`), OpenAPI
    files (`/openapi/...`), `/healthz`; `static/brand/` comes from the ISCC brand kit, `static/vendor/` is Stoplight
    Elements (do not edit either)
- `iscc_c2pa_store/middleware.py` - body limits (413), CORS for the public API, operator login rate limit
- `iscc_c2pa_store/throttle.py` - fixed-window rate limits per client (IPv6 by /64), rates read from settings
- `docker/entrypoint.sh`, `Dockerfile` - migrations, the declaration retry loop, gunicorn (options in
    `GUNICORN_CMD_ARGS`)
- `tests/` - pytest; `conftest.py` signs real Manifest Stores with a throwaway certificate chain and provides a fake
    ISCC hub that checks signatures; `tests/data/` holds Manifest Stores written by c2pa-rs; `tests/settings.py`
    fixes the test environment
- `docs/`, `zensical.toml` - documentation site on the ISCC theme

## Rules

- The API is spec-first: change `openapi.yaml` first, run `uv run poe codegen`, then the code. Never edit `schema/`,
    `c2pa-sbr.json` or `vendor/` by hand.
- `api.py` stays thin; logic lives in `store.py` and the pure modules. Pure modules never import Django.
- Error statuses follow the C2PA OpenAPI: invalid parameters and bodies are `400` (never `422`), unknown manifests
    are `404`, deleted ones `410`.
- Uploaded bytes are untrusted: they reach c2pa-rs only through `manifest.read`, and nothing else parses them.
- The store never fetches URLs from requests or manifests, and `manifest.context` keeps c2pa-rs off the network.
    Its only outbound HTTP is to the configured hub.
- HTTP client is `httpx2` (with `httpcore2`), not `httpx`.
- Type hints as PEP 484 type comments; annotations only where django-ninja or pydantic need them.
- Short pure functions, no nested functions, a docstring on every module and function.
- Tests use real data: Manifest Stores signed in `conftest.py`, IEP-0020 test vectors, the fake hub. No mocks of our
    own code.
- The C2PA specification repository (specs-core) is private. The only content copied from it is the CC BY 4.0
    OpenAPI subset in `c2pa-sbr.json`. Do not copy other specification text into this public repository; link to
    spec.c2pa.org instead.
- Commits follow Conventional Commits (`feat:`, `fix:`, `build:`, `docs:`, `ci:`, `test:`).
