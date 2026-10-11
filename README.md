# ISCC C2PA Store

[![CI](https://github.com/iscc/iscc-c2pa-store/actions/workflows/ci.yml/badge.svg)](https://github.com/iscc/iscc-c2pa-store/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)

> [!WARNING]
> **Experimental (beta, MVP).** This service and its documentation are provided as is, without warranty of any kind. The API, the declaration behaviour and the public instances may change in breaking ways before version 1.0.

A C2PA Manifest Repository with a
[C2PA Soft Binding Resolution API](https://spec.c2pa.org/specifications/specifications/2.4/softbinding/Decoupled.html).
It stores C2PA Manifest Stores, serves them byte for byte at their manifest address, and finds them again by soft
binding: exact match for every algorithm on the C2PA Soft Binding Algorithm List, ISCC similarity for `io.iscc.v0`.
It also declares the `io.iscc.v0` soft binding of each upload on the ISCC Discovery Protocol under its own did:web
key, with the manifest address as gateway URL, so that [iscc-c2pa-resolver](https://github.com/iscc/iscc-c2pa-resolver)
finds the manifest network-wide.

| Network | Store                             | ISCC hub                  |
| ------- | --------------------------------- | ------------------------- |
| Mainnet | `https://c2pa-store.iscc.io`      | `https://iscc.id`         |
| Testnet | `https://c2pa-store-test.iscc.io` | `https://staging.iscc.id` |

## Quick look

Publish a C2PA Manifest Store (a `.c2pa` sidecar, not the asset):

```bash
curl -X POST "https://c2pa-store-test.iscc.io/v1/manifests" \
    -H "Content-Type: application/c2pa" --data-binary @photo.c2pa
```

```json
{
  "manifestId": "urn:c2pa:F9168C5E-CEB2-4FAA-B6BF-329BF39FA1E4",
  "manifestUrl": "https://c2pa-store-test.iscc.io/v1/manifests/urn:c2pa:F9168C5E-CEB2-4FAA-B6BF-329BF39FA1E4",
  "declaration": {
    "status": "declared",
    "isccId": "ISCC:MAIGKV5FAAXOXYAB",
    "hub": "https://staging.iscc.id"
  }
}
```

Find it again by soft binding, then fetch it from `GET /v1/manifests/{manifestId}`:

```bash
curl "https://c2pa-store-test.iscc.io/v1/matches/byBinding?alg=io.iscc.v0&value=<percent-encoded base64 ISCC-SEQ>"
```

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

## What it implements

- `POST /v1/manifests`: accepts a Manifest Store that c2pa-rs reads, without network access, and whose active
    manifest has a verified claim signature; hard-binding failures (no asset) and untrusted or expired signing
    credentials are recorded, not refused; bytes after the Manifest Store and tampered ingredient manifests are
    refused; manifest IDs are first come, first served
- `GET /v1/manifests/{manifestId}`: the stored bytes, unchanged; `DELETE` with the operator token deletes them and
    withdraws the declaration; every `GET` route also answers `HEAD`
- `GET` and `POST /v1/matches/byBinding`, with `io.iscc.v0` values as specified in
    [IEP-0020](https://ieps.iscc.codes/iep-0020/) and the score rule of iscc-c2pa-resolver
- `GET /v1/services/supportedAlgorithms`, `/capabilities` (`storeManifests`), `/status` and
    `/.well-known/c2pa-soft-binding-resolution`; the C2PA Soft Binding API conformance harness passes its discovery,
    service and unknown-manifest checks (see [Conformance](https://c2pa-store.iscc.codes/conformance/) for what it
    covers)
- Extensions: the `declare` upload parameter, the `manifestUrl`, `declaration` and `isccId` result fields, and the
    store's DID document at `/.well-known/did.json`
- An operator dashboard at `/admin/` and an API reference at `/docs`

See the [documentation](https://c2pa-store.iscc.codes/) for the API, declarations, conformance, operations and
development.

## Development

```bash
uv sync
uv run prek install
cp .env.example .env
uv run poe all      # format, type check, test with 100% coverage
uv run poe migrate
uv run poe serve    # http://127.0.0.1:45470
```

## License

Apache-2.0. Documentation CC BY 4.0.
