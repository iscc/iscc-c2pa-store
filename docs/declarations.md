---
title: Declarations
description: The signed IsccNotes the ISCC C2PA Store sends to its ISCC hub, their statuses, retries and withdrawals
icon: lucide/signature
---

# Declarations

For every upload with a usable `io.iscc.v0` soft binding, the store declares the content on the
[ISCC Discovery Protocol](https://ieps.iscc.codes/iep-0013/) under its own key, with the manifest address as
gateway URL. Anyone who later finds the content through the ISCC network is led to the manifest.
{ .iscc-lead }

## Identity

The store signs as `did:web:<host>`, where `<host>` is the host of `ISCC_C2PA_STORE_URL` (a port is
percent-encoded, as did:web requires). Its DID document, with the Ed25519 public key, is served at
`/.well-known/did.json`:

```json
{
  "id": "did:web:c2pa-store-test.iscc.io",
  "verificationMethod": [
    {
      "id": "did:web:c2pa-store-test.iscc.io#z6MkkArwN9vYBX3y1XQGo67D21UuvMd4m8sBM4r6mPbq9Fjg",
      "type": "Multikey",
      "controller": "did:web:c2pa-store-test.iscc.io",
      "publicKeyMultibase": "z6MkkArwN9vYBX3y1XQGo67D21UuvMd4m8sBM4r6mPbq9Fjg"
    }
  ],
  "authentication": [
    "did:web:c2pa-store-test.iscc.io#z6MkkArwN9vYBX3y1XQGo67D21UuvMd4m8sBM4r6mPbq9Fjg"
  ],
  "assertionMethod": [
    "did:web:c2pa-store-test.iscc.io#z6MkkArwN9vYBX3y1XQGo67D21UuvMd4m8sBM4r6mPbq9Fjg"
  ]
}
```

The document is abbreviated here; it also lists the key under `capabilityDelegation` and `capabilityInvocation`.
Declarations are enabled when the deployment has both a secret key (`ISCC_C2PA_STORE_SECKEY`) and a hub
(`ISCC_C2PA_STORE_HUB_URL`). Without them the store hosts and finds manifests only, every upload gets the status
`disabled`, and `/.well-known/did.json` answers `404`.

## What gets declared

The units come from the `io.iscc.v0` soft bindings of the active manifest that describe the whole asset, as the
store indexed them (256-bit Version 0 units, Meta-Codes left out). A block with a `scope`, a region or time span of
the asset, is searchable on the store but never declared, because a declaration stands for the whole content. The
store takes the first unit of each MainType, in the order Semantic-, Content-, Data-, Instance-Code:

- a 256-bit Data-Code and Instance-Code are required ([IEP-0020](https://ieps.iscc.codes/iep-0020/)); without
    them the declaration is `skipped`;
- a Semantic-Code whose SubType differs from the Content-Code's is left out, because an ISCC-CODE cannot combine
    them.

## The IsccNote

```json
{
  "$schema": "http://purl.org/iscc/schema/iscc-note-0.8.0.json",
  "iscc_code": "ISCC:KAA2RASIYU5IKLENAW2B6NDSJEZCLJJCV5FCZHF7NI",
  "datahash": "1e20a522af4a2c9cbf6a808924afd2acfa0793347b936678f5982e22fdd3dd4a0e37",
  "nonce": "0010dc860d4568c8ae7ac3e8ed3b2946",
  "timestamp": "2026-10-09T12:00:00.000Z",
  "gateway": "https://c2pa-store-test.iscc.io/v1/manifests/urn:c2pa:F9168C5E-CEB2-4FAA-B6BF-329BF39FA1E4",
  "units": [
    "ISCC:EAD2RASIYU5IKLENP2OFI4CHZGRWYQCSW2WKX3Y6FJGOCXSYNYGLGBI",
    "ISCC:GADQLNA7GRZESMRF2J7NZPNWGI3II2ST5YUN5SS6GVQ2ZQGJXPPYDNI"
  ],
  "signature": {
    "version": "ISCC-SIG v1.0",
    "controller": "did:web:c2pa-store-test.iscc.io",
    "pubkey": "z6MkkArwN9vYBX3y1XQGo67D21UuvMd4m8sBM4r6mPbq9Fjg",
    "proof": "z3LNSaxb..."
  }
}
```

The example declares the Content-, Data- and Instance-Code of the API reference; the proof is shortened.

| Field       | Value                                                                             |
| ----------- | --------------------------------------------------------------------------------- |
| `$schema`   | IsccNote schema 0.8.0                                                             |
| `iscc_code` | The ISCC-CODE composed from all declared units                                    |
| `datahash`  | `1e20` followed by the hex body of the Instance-Code                              |
| `nonce`     | A fresh nonce whose first 12 bits name the hub of the declaration                 |
| `timestamp` | Signing time, RFC 3339 UTC with milliseconds                                      |
| `gateway`   | The manifest address                                                              |
| `units`     | The declared units without the Instance-Code                                      |
| `signature` | ISCC signature ([IEP-0019](https://ieps.iscc.codes/iep-0019/)) by the store's key |

The `datahash` is a BLAKE3 multihash of the asset. The store never sees the asset, but it does not need to: the
body of a 256-bit Instance-Code is the BLAKE3 digest of the asset, so the multihash prefix `1e20` (BLAKE3, 32
bytes) and the Instance-Code body give the same value.

The note goes to `POST {hub}/declaration` with the header `X-Force-Declaration: true`. Others may have declared the
same content before, which the ISCC Discovery Protocol allows, and the store declares its own manifest address
regardless.

Each declaration remembers its hub and HUB-ID (`ISCC_C2PA_STORE_HUB_URL` and `ISCC_C2PA_STORE_HUB_ID` when the
upload was stored). Retries and the withdrawal go to that hub with nonces for it, so a later change of the
configured hub does not strand earlier declarations.

## Statuses

The status is part of every upload answer (`declaration.status`) and of the operator dashboard.

| Status      | Meaning                                                                                                           |
| ----------- | ----------------------------------------------------------------------------------------------------------------- |
| `declared`  | The hub accepted the note; `isccId` is the ISCC-ID of the declaration                                             |
| `pending`   | Hub unreachable, server error (`5xx`), `408`, `425`, `429`, or `201` without an ISCC-ID; the store retries        |
| `failed`    | The hub refused the note (any other non-`201` answer), or the retries gave up; the error is kept for the operator |
| `skipped`   | No 256-bit Data-Code and Instance-Code in a whole-asset `io.iscc.v0` binding, or the upload sent `declare=false`  |
| `disabled`  | The deployment has no declaration key or no hub                                                                   |
| `withdrawn` | The manifest was deleted, and the declaration was removed from the hub or had no ISCC-ID to remove                |

The first attempt happens during the upload, after the manifest is stored, so a hub that is down never loses a
manifest. Uploading the same bytes again returns the stored result and does not declare again.

## Retries

The container runs `python manage.py retry_declarations` every `ISCC_C2PA_STORE_RETRY_SECONDS` (120 by default).
Each round sends every due `pending` declaration of a live manifest to the hub again, and the withdrawal of every
due declaration that is still `declared` although its manifest was deleted.

Retries back off. A declaration is due once a wait has passed since its last change: 60 seconds, doubled with
every hub request it has made, at most one hour. The 60 seconds also keep a round from sending a note that an
upload or another round is still sending. A successful declaration starts counting again from zero for its
withdrawal. Declarations held up by a hub outage are sent within about an hour of the hub's return.

A declaration still `pending` 7 days after its manifest was uploaded becomes `failed` with the error
`Gave up after 7 days`. Withdrawals never give up: one the hub keeps refusing stays `declared` with its last error
and is tried again with the same backoff, at the longest once an hour.

The Declarations page of the operator dashboard (`/admin/`) has an action for the declarations that need
attention: it makes selected `failed` declarations `pending` again and sends them to the hub at once, sends
selected `pending` ones, and withdraws those still `declared` for a deleted manifest.

`GET /v1/services/status` reports `degraded` while any declaration of a manifest uploaded more than 15 minutes ago
is still `pending`. It measures the age of the upload, not how long the declaration has been pending: a `failed`
declaration that an operator sends again turns the status `degraded` at once if the hub is still unavailable.

## Deletes withdraw

A delete, through `DELETE /v1/manifests/{manifestId}` with the operator token or the delete action of the operator
dashboard, removes the bytes and the index entries and keeps a tombstone: the address answers `410`, and neither
the same bytes nor the same manifest ID can be uploaded again (`409`) until an operator releases the ID.

| Status before the delete | What happens                                                                           |
| ------------------------ | -------------------------------------------------------------------------------------- |
| `declared`               | A signed IsccNoteDelete goes to `DELETE {hub}/declaration/{iscc_id}`; then `withdrawn` |
| `pending` or `failed`    | Becomes `withdrawn` in the same transaction as the delete, without contacting the hub  |
| any other                | Unchanged                                                                              |

The hub's `204`, or `404` for a declaration it no longer has, counts as withdrawn. If the hub cannot be reached or
refuses, or the store no longer has its declaration key, the declaration stays `declared` with the error, and the
retry loop sends the withdrawal again.

A `pending` declaration is not always absent from the hub:

- A request may be in flight while the manifest is deleted. If the hub accepts it, the store records the
    declaration as `declared` all the same, and the retry loop withdraws it.
- The hub may have accepted an earlier note whose answer never arrived: a timeout, or a `201` without an ISCC-ID.
    The store does not learn the ISCC-ID of such a note, so it can neither record nor withdraw it. The retry sends
    a fresh note, which the hub may accept as a second declaration, and after a delete the unrecorded note stays on
    the hub with a gateway URL that answers `410`.

### Releasing a manifest ID

A tombstone keeps its manifest ID taken. An operator who deleted a manifest uploaded under someone else's manifest
ID can give the ID back with the dashboard action "Release the IDs of deleted manifests for new uploads". It
removes the tombstone and its declaration record: the address then answers `404`, and the ID, as well as the
deleted bytes, can be uploaded again. The action skips manifests that are not deleted and those whose declaration
is still `declared`, because the store would lose track of a declaration it still has to withdraw.

## Declaring it yourself

Uploaders that declare the content under their own identity send `declare=false`:

```bash
curl -X POST "https://c2pa-store-test.iscc.io/v1/manifests?declare=false" \
    -H "Content-Type: application/c2pa" \
    --data-binary @photo.c2pa
```

The answer has the status `skipped` and the `manifestUrl`. Use that address as the gateway URL of your own
declaration; ISCC C2PA resolvers then find the manifest through your declaration, as their
[gateway contract](https://c2pa-resolver.iscc.codes/gateway/) describes.
