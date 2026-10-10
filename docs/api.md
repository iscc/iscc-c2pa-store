---
title: API reference
description: Routes, extensions and errors of the ISCC C2PA Store, a C2PA Soft Binding Resolution API
icon: lucide/braces
---

# API reference

!!! warning "Experimental"

    This beta MVP and its documentation are provided as is, without warranty of any kind. The API, the declaration behaviour and the public instances may change in breaking ways before version 1.0.

The store implements the store, fetch, query and service routes of the
[C2PA Soft Binding Resolution API](https://spec.c2pa.org/specifications/specifications/2.4/softbinding/Decoupled.html)
as defined for the upcoming C2PA specification 2.5, whose OpenAPI document still carries version `2.4.0`. Every
instance serves an interactive API reference at `/docs` and the OpenAPI document at `/openapi/openapi.yaml` (also
as `/openapi/openapi.json`). Schemas named `c2pa.*` in that document are copied verbatim from the C2PA OpenAPI
definition (`/openapi/c2pa-sbr.json`, CC BY 4.0).
{ .iscc-lead }

## Routes

| Route                                           | Behaviour                                                                               |
| ----------------------------------------------- | --------------------------------------------------------------------------------------- |
| `POST /v1/manifests`                            | Store a C2PA Manifest Store sent as `application/c2pa` and declare it                   |
| `GET /v1/manifests/{manifestId}`                | The stored Manifest Store, byte for byte, as `application/c2pa`                         |
| `DELETE /v1/manifests/{manifestId}`             | Delete a manifest and withdraw its declaration; needs the operator token                |
| `GET /v1/matches/byBinding`                     | Query with `alg`, `value` and optional `maxResults`                                     |
| `POST /v1/matches/byBinding`                    | Same query with `{"alg": ..., "value": ...}` as JSON body                               |
| `GET /v1/services/supportedAlgorithms`          | `io.iscc.v0` and every algorithm the store has indexed, by fingerprint and watermark    |
| `GET /v1/services/capabilities`                 | `info.version` of `c2pa-sbr.json` (`2.4.0`) and the capability `storeManifests`         |
| `GET /v1/services/status`                       | `ok`, or `degraded` while the declaration of an upload older than 15 minutes is pending |
| `GET /.well-known/c2pa-soft-binding-resolution` | The API base, the capabilities and status addresses, and the specification version      |
| `GET /.well-known/did.json`                     | Extension: the did:web document of the declaration key; `404` when not declaring        |

Every `GET` route also answers `HEAD`. Not implemented: `byContent`, `byReference`, the bindings and receipt
routes.

The discovery document gives absolute addresses:

```json
{
  "apiEndpoint": "https://c2pa-store-test.iscc.io/v1",
  "c2paSpecificationVersion": "2.4.0",
  "capabilitiesEndpoint": "https://c2pa-store-test.iscc.io/v1/services/capabilities",
  "statusEndpoint": "https://c2pa-store-test.iscc.io/v1/services/status"
}
```

Reads and queries are open. Uploads need no token. Each client has two separate rate limits: uploads (10 per
minute by default) and fetches and queries together (120 per minute by default), so reading never uses up the
upload allowance. A client is its IPv4 address or its IPv6 /64 network. Both limits are counted in fixed windows of
the configured period; the service routes are not limited, and neither are the addresses the operator exempts, such
as its own resolvers.

Deletes need the operator token as `Authorization: Bearer <token>`. Public API routes send
`Access-Control-Allow-Origin: *`, errors and refused bodies included, so web pages of any origin can upload, fetch
and query; `DELETE` is not allowed cross-origin.

## Publish a manifest

```bash
curl -X POST "https://c2pa-store-test.iscc.io/v1/manifests" \
    -H "Content-Type: application/c2pa" \
    --data-binary @photo.c2pa
```

The body is a bare C2PA Manifest Store (a `.c2pa` sidecar), not the asset. Uploads are capped at 2 MB by default.

```json
{
  "manifestId": "urn:c2pa:F9168C5E-CEB2-4FAA-B6BF-329BF39FA1E4",
  "manifestUrl": "https://c2pa-store-test.iscc.io/v1/manifests/urn%3Ac2pa%3AF9168C5E-CEB2-4FAA-B6BF-329BF39FA1E4",
  "declaration": {
    "status": "declared",
    "isccId": "ISCC:MAIGKV5FAAXOXYAB",
    "hub": "https://staging.iscc.id"
  }
}
```

| Field                | Meaning                                                                              |
| -------------------- | ------------------------------------------------------------------------------------ |
| `manifestId`         | Label of the active manifest: the ID under which the store serves the Manifest Store |
| `manifestUrl`        | Extension: the manifest address, also the gateway URL of the declaration             |
| `declaration.status` | Extension: `declared`, `pending`, `failed`, `skipped`, `disabled` or `withdrawn`     |
| `declaration.isccId` | Extension: ISCC-ID of the declaration, once `declared`                               |
| `declaration.hub`    | Extension: the ISCC hub of the declaration                                           |

The answer is `200` for a first upload and for the same bytes uploaded again. Manifest IDs are first come, first
served: other bytes under a taken ID, or under the ID of a deleted manifest, get `409` until an operator releases
the ID. The store accepts manifest IDs of at most 255 printable characters; a Manifest Store whose active manifest
has a longer or unprintable label is refused with `400` and the code `manifest.idInvalid`. A Manifest Store with
more than 64 indexable soft-binding blocks, more than 8 `io.iscc.v0` blocks or more than 32 distinct ISCC-UNITs is
refused with `400` and the code `manifest.tooManyBindings` ([Index](how-it-works.md#2-index)). Status values are
explained on the [Declarations](declarations.md#statuses) page.

### Query parameters

| Parameter       | Default            | Meaning                                                                 |
| --------------- | ------------------ | ----------------------------------------------------------------------- |
| `declare`       | deployment setting | Extension: `false` stores and indexes the manifest without declaring it |
| `returnReceipt` | `false`            | Accepted and ignored: the store issues no receipts                      |

Send `declare=false` when you declare the content on the ISCC Discovery Protocol yourself, with `manifestUrl` as
your gateway URL ([Declarations](declarations.md#declaring-it-yourself)). The public instances declare by default.

## Fetch a manifest

```bash
curl -o photo.c2pa \
    "https://c2pa-store-test.iscc.io/v1/manifests/urn:c2pa:F9168C5E-CEB2-4FAA-B6BF-329BF39FA1E4"
```

The answer is the Manifest Store exactly as uploaded, as `application/c2pa`, with the SHA-256 of the bytes as
`ETag` and `Cache-Control: public, max-age=300`. A request whose `If-None-Match` lists that tag (weak or strong) or
`*` gets `304`. `HEAD` answers with the same headers and no body.
`returnActiveManifest` is accepted and ignored; the full Manifest Store is always returned.

## Query by binding

`alg` is any algorithm on the
[C2PA Soft Binding Algorithm List](https://github.com/c2pa-org/softbinding-algorithm-list); `value` is the base64
binding value, percent-encoded in a `GET` request. For `io.iscc.v0` the value is an ISCC-SEQ as specified in
[IEP-0020](https://ieps.iscc.codes/iep-0020/).

```bash
curl "https://c2pa-store-test.iscc.io/v1/matches/byBinding?alg=io.iscc.v0&value=<percent-encoded base64 ISCC-SEQ>"
```

```python
import base64
from urllib.parse import quote

import iscc_core as ic

units = [
    "ISCC:EAD2RASIYU5IKLENP2OFI4CHZGRWYQCSW2WKX3Y6FJGOCXSYNYGLGBI",  # Content-Code
    "ISCC:GADQLNA7GRZESMRF2J7NZPNWGI3II2ST5YUN5SS6GVQ2ZQGJXPPYDNI",  # Data-Code
    "ISCC:IAD2KIVPJIWJZP3KQCESJL6SVT5APEZUPOJWM6HVTAXCF7OT3VFA4NY",  # Instance-Code
]
value = quote(base64.b64encode(ic.encode_seq(units)).decode("ascii"), safe="")
url = f"https://c2pa-store-test.iscc.io/v1/matches/byBinding?alg=io.iscc.v0&value={value}&maxResults=5"
```

Response:

```json
{
  "matches": [
    {
      "manifestId": "urn:c2pa:F9168C5E-CEB2-4FAA-B6BF-329BF39FA1E4",
      "similarityScore": 100,
      "isccId": "ISCC:MAIGKV5FAAXOXYAB"
    }
  ]
}
```

| Field             | Meaning                                                                                                                     |
| ----------------- | --------------------------------------------------------------------------------------------------------------------------- |
| `manifestId`      | Fetch the manifest from `GET /v1/manifests/{manifestId}` on the same store                                                  |
| `similarityScore` | 100 for an exact match or an equal Instance-Code, else the best unit similarity ([score](how-it-works.md#similarity-score)) |
| `isccId`          | Extension: ISCC-ID of the store's declaration of this manifest, when it has one                                             |

Matches carry no `endpoint`, because every match is stored here. An `io.iscc.v0` value may hold at most 32
searchable units. Equal scores rank manifests with a trusted signing credential first, then earlier uploads.
`maxResults` defaults to 10; values above 100 are capped at 100. For values too long for a URL, send
`POST /v1/matches/byBinding` with `{"alg": "io.iscc.v0", "value": "<base64>"}` as JSON.

## Errors

| Status | When                                                                                                                            |
| ------ | ------------------------------------------------------------------------------------------------------------------------------- |
| `400`  | Invalid parameter, body or `Content-Length`; algorithm not on the C2PA list; invalid base64 or ISCC-SEQ; refused Manifest Store |
| `401`  | `DELETE` without a valid operator token (`WWW-Authenticate: Bearer`)                                                            |
| `404`  | Unknown manifest ID (any longer than 255 characters or unprintable too) or API path; `/.well-known/did.json` when not declaring |
| `409`  | Upload whose manifest ID is taken by other bytes, or of a deleted manifest                                                      |
| `410`  | Manifest that was deleted                                                                                                       |
| `413`  | Upload larger than the upload limit (2 MB by default), or another request body larger than 64 KB                                |
| `415`  | Upload not sent as `application/c2pa`                                                                                           |
| `429`  | Rate limit of the client address exceeded; `Retry-After` says how long to wait                                                  |

Error bodies are JSON with a `detail` member. A refused Manifest Store also carries `code`: the C2PA validation
status code of the first failure, or `manifest.unreadable`, `manifest.missing`, `manifest.idInvalid`,
`manifest.tooManyBindings` or `claimSignature.missing`. Unknown paths under `/v1/` and `/.well-known/` answer
`{"detail": "Not found"}`.

```json
{
  "detail": "The active manifest failed validation",
  "code": "assertion.hashedURI.mismatch"
}
```
