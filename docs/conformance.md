---
title: Conformance
description: How the ISCC C2PA Store follows the C2PA Soft Binding Resolution API, where it departs, and what the conformance harness checks
icon: lucide/badge-check
---

# Conformance

The store implements the
[C2PA Soft Binding Resolution API](https://spec.c2pa.org/specifications/specifications/2.4/softbinding/Decoupled.html)
of the upcoming C2PA specification 2.5, whose OpenAPI document still carries version `2.4.0`. This page lists what
it implements, where it departs from the C2PA OpenAPI definition, and how it is tested against it.
{ .iscc-lead }

## Implemented

| Group   | Routes                                                                                                          |
| ------- | --------------------------------------------------------------------------------------------------------------- |
| Store   | `POST /v1/manifests`, `DELETE /v1/manifests/{manifestId}`                                                       |
| Fetch   | `GET` and `HEAD /v1/manifests/{manifestId}`                                                                     |
| Query   | `GET` and `POST /v1/matches/byBinding`                                                                          |
| Service | `GET /v1/services/supportedAlgorithms`, `/capabilities`, `/status`, `/.well-known/c2pa-soft-binding-resolution` |

`/v1/services/capabilities` reports `c2paSpecificationVersion` as the `info.version` of the C2PA OpenAPI subset the
store implements (`2.4.0`), and the one optional capability it has: `storeManifests`. The discovery document at
`/.well-known/c2pa-soft-binding-resolution` gives `apiEndpoint`, `capabilitiesEndpoint` and `statusEndpoint` as
absolute URLs. Every `GET` route also answers `HEAD`.

Not implemented: `byContent`, `byReference`, the bindings routes and receipts.

The served contract, `/openapi/openapi.yaml`, references the C2PA schemas in `/openapi/c2pa-sbr.json`, a verbatim
copy (CC BY 4.0) of the operations the store implements. The test suite checks that every C2PA operation keeps its
operationId, parameters and response schemas in the store's contract.

## Deviations

| Topic                  | C2PA OpenAPI                                    | The store                                                                                   |
| ---------------------- | ----------------------------------------------- | ------------------------------------------------------------------------------------------- |
| Uploads                | OAuth 2.0 with the scope `store:manifests`      | Anonymous, limited per client address                                                       |
| Fetches and queries    | OAuth 2.0 with the scope `fetch:manifests`      | Open, limited per client address                                                            |
| Deletes                | OAuth 2.0 with the scope `store:manifests`      | The operator's bearer token; `401` with `WWW-Authenticate: Bearer` without it               |
| `returnReceipt`        | Requests a receipt for the stored manifest      | Accepted and ignored; no receipts                                                           |
| `returnActiveManifest` | Requests only the active manifest               | Accepted and ignored; the full Manifest Store is returned                                   |
| `declare`              | Not defined                                     | Extension of `POST /v1/manifests`: whether the store declares the ISCC soft binding         |
| Response fields        | `manifestId`; `manifestId`, `similarityScore`   | Extension fields `manifestUrl` and `declaration` on uploads, `isccId` on matches            |
| Further statuses       | `200`, `204`, `400`, `403`, `404`, `414`, `500` | Also `409` (ID taken), `410` (deleted), `413` (too large), `415` (media type), `429` (rate) |

Matches never carry an `endpoint`: every match is stored in the same repository, and the C2PA API defines that a
match without `endpoint` is fetched from the API that answered the query.

## Soft-binding values written as text

The C2PA specification defines the value of a soft-binding block as a byte string, and c2pa-rs reports byte strings
as base64 text. The store therefore reads a text value as base64 and indexes the decoded bytes, which is what queries
send. Some manifests in circulation write the value as plain text instead (for example hex or binary digits). Such a
value cannot be told apart from base64 in the c2pa-rs report: text that is not valid base64 is not indexed, and text
that happens to be valid base64 is indexed as the bytes it decodes to, so an exact-match query for it may not find it.

## Known issue in the C2PA schema

The C2PA schema `c2pa.softBindingAlgList` places its `oneOf` (either `watermarks` or `fingerprints` required) on the
items of `watermarks` instead of on the list itself. A strict validator therefore rejects every non-empty
`watermarks` list, although each entry has the `alg` the specification intends. `GET /v1/services/supportedAlgorithms`
answers with the intended shape, so its answer fails strict validation against the verbatim schema as soon as the
store has indexed a watermark.

## Conformance harness

The [Cognitive Proof Soft Binding API conformance harness](https://www.npmjs.com/package/@cognitiveproof/softbinding-api-conformance)
(version 1.0.0) runs against the container image in CI and locally with `uv run poe conformance`
(`scripts/conformance.sh`, needs Node).

The harness decides which tests to run from the capabilities the store reports. `scripts/conformance.sh` removes
`storeManifests` from them (see below), so the harness runs only its tests that need no capability:

| Harness suite                                   | Tests run                                                                  | Result  |
| ----------------------------------------------- | -------------------------------------------------------------------------- | ------- |
| Service discovery endpoints                     | `capabilities`, `status`, `supportedAlgorithms` and the discovery document | passes  |
| `supportedAlgorithms` shape and agreement       | Every entry has an `alg`; capabilities and discovery report one version    | passes  |
| `GET /manifests/:manifestId`                    | An unknown manifest ID answers `404`                                       | passes  |
| Auth on `GET /manifests/:manifestId`            | An unauthenticated fetch answers `200`, `401`, `403` or `404`              | passes  |
| Store round trip, auth on store routes, receipt | Need `storeManifests`                                                      | skipped |
| Bindings                                        | Need `storeBindings` and `storeManifests`                                  | skipped |

Harness 1.0.0 queries `/matches/byBinding` only inside its bindings suite, so the query routes have no harness
coverage, and it never fetches a stored manifest: its fetch checks are the `404` for an unknown ID and the status
of an unauthenticated fetch. Queries, fetches of stored manifests and the
store routes are covered by the test suite of the repository instead, with Manifest Stores signed by c2pa-python.

`storeManifests` is withheld because the store suites assume a different store: they post plain text as
`application/c2pa` and expect `401` without a token, and they expect `404` for a deleted manifest. The store
refuses such a body with `400`, as its contract documents for anything that is not an acceptable Manifest Store,
accepts anonymous uploads, and answers `410` for a deleted manifest.

Harness 1.0.0 cannot run from its npm package as published; the script installs it into a temporary directory and
runs its tests from there.
