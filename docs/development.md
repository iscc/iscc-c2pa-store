---
title: Development
description: Set up, test and release the ISCC C2PA Store
icon: lucide/code
---

# Development

Python 3.12+, [uv](https://docs.astral.sh/uv/) for environments and [poe](https://poethepoet.natn.io/) for tasks.
Django and django-ninja serve the API, c2pa-python (c2pa-rs) reads Manifest Stores, iscc-core and iscc-crypto build
and sign declarations.
{ .iscc-lead }

## Setup

```bash
uv sync                    # dev and docs dependencies
uv run prek install        # git hooks: ruff, mdformat, lockfile, workflow, OpenAPI, migrations and type checks
cp .env.example .env       # local settings for the dev server
uv run poe migrate
uv run poe serve           # http://127.0.0.1:45470
```

Without `ISCC_C2PA_STORE_SECKEY` and `ISCC_C2PA_STORE_HUB_URL` in `.env` the dev server stores and finds manifests
but does not declare them. To declare against the testnet hub, create a key with `uvx iscc-crypto keygen` and set
`ISCC_C2PA_STORE_HUB_URL=https://staging.iscc.id`; the hub then sees the controller `did:web:127.0.0.1%3A45470`.

## Tasks

| Command                      | What it does                                                                       |
| ---------------------------- | ---------------------------------------------------------------------------------- |
| `uv run poe all`             | Format, type check and test (the local loop)                                       |
| `uv run poe ci`              | Lint, type check, test and audit dependencies, changing nothing                    |
| `uv run poe format`          | ruff format and fix, mdformat for `README.md`, `CLAUDE.md`, `SECURITY.md`, `docs/` |
| `uv run poe lint`            | Check formatting, lint rules and missing migrations                                |
| `uv run poe typecheck`       | pyright                                                                            |
| `uv run poe test`            | Tests with the 100% branch coverage gate                                           |
| `uv run poe audit`           | Check locked dependencies for known vulnerabilities                                |
| `uv run poe migrate`         | Apply migrations to the local dev database (settings from `.env`)                  |
| `uv run poe serve`           | Dev server on `http://127.0.0.1:45470` (settings from `.env`)                      |
| `uv run poe conformance`     | C2PA conformance harness against the dev server (needs Node)                       |
| `uv run poe codegen`         | Generate `openapi.json` and the pydantic models from `openapi.yaml`                |
| `uv run poe sync-c2pa <dir>` | Copy the implemented subset of the C2PA OpenAPI from a specs-core checkout         |
| `uv run poe vendor-c2pa`     | Refresh the vendored C2PA Soft Binding Algorithm List and trust list               |
| `uv run poe docs-serve`      | Documentation preview on `http://127.0.0.1:45471`                                  |
| `uv run poe docs-build`      | Build the documentation site into `site/`                                          |
| `uv run poe docs-check`      | Check the built site against the ISCC theme rules                                  |

## API contract

The API is spec-first. `iscc_c2pa_store/openapi/openapi.yaml` is written by hand; its `c2pa.*` schemas are
references into `c2pa-sbr.json`, a verbatim subset of the C2PA Soft Binding Resolution API definition.

1. Change `openapi.yaml`.
1. Run `uv run poe codegen`: it writes `openapi.json` and regenerates the pydantic models in
    `iscc_c2pa_store/schema/`.
1. Change the code. `api.py` maps routes to HTTP only; the logic lives in `store.py` and in the pure modules, which
    never import Django.

Never edit `schema/`, `c2pa-sbr.json` or `vendor/` by hand. After `uv run poe sync-c2pa`, the git diff of
`c2pa-sbr.json` shows what changed upstream. Invalid parameters and bodies answer `400`, never `422`, as the C2PA
OpenAPI documents.

## Test data

The tests use real data, no mocks of the store's own code:

- **Manifest Stores** are signed by c2pa-python during each test session with a throwaway certificate chain (a CA
    and an ES256 signer). Tests that add the CA to the trust anchors cover trusted uploads, the others untrusted
    ones. Soft bindings carry the [IEP-0020](https://ieps.iscc.codes/iep-0020/) test vectors or fresh ISCC-UNITs.
- **Manifest Stores written by c2pa-rs** in `tests/data/`: one without soft bindings, and one with a CAWG
    identity assertion, extracted from the c2pa-rs test fixture `C_with_CAWG_data.jpg` (MIT or Apache-2.0). Its
    signing credential has expired, so it is refused, but without any `cawg.*` validation code.
- **A fake ISCC hub** replaces the network: it checks the signature of every IsccNote and IsccNoteDelete, requires
    `X-Force-Declaration`, issues ISCC-IDs, and can refuse, fail or go down to exercise every declaration status.

## Quality gates

Every pull request and every push to `main` runs:

- the prek hooks: ruff (format and lint, including security and complexity rules), mdformat, the lockfile check,
    zizmor for GitHub Actions security, openapi-spec-validator, the check that `openapi.json` and the models match
    `openapi.yaml`, the migrations check and pyright;
- `uv audit` against known vulnerabilities in locked dependencies;
- pytest with 100% branch coverage on Python 3.12, 3.13 and 3.14;
- after all of the above, a container build that must become healthy, run as uid 10001, serve its pages and pass
    the C2PA Soft Binding API conformance harness ([Conformance](conformance.md)). On `main`, this job pushes the
    image it tested as `main` and `sha-<commit>`; nothing is rebuilt for publishing.

Changes to `docs/` and `zensical.toml` also build and check this site; pushes to `main` publish it.

## Release

1. Bump `version` in `pyproject.toml` (and `info.version` in `openapi.yaml`) on `main` and wait for CI to publish
    the `sha-<sha>` image.
1. Create a GitHub Release `vX.Y.Z` on that commit. The release workflow checks that the tag matches the version
    and retags the tested image as `X.Y.Z`, and as `latest` unless it is a pre-release. Nothing is rebuilt, so
    mainnet runs exactly the bytes that testnet ran.
