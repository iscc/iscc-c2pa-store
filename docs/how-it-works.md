---
title: How it works
description: How the ISCC C2PA Store checks, stores, indexes, serves and declares C2PA Manifest Stores
icon: lucide/workflow
---

# How it works

The store is a C2PA Manifest Repository: it keeps Manifest Stores, never the assets they describe. It answers the
C2PA Soft Binding Resolution API for the manifests it holds, and makes them findable network-wide by declaring
their ISCC soft bindings on the ISCC Discovery Protocol (IDP).
{ .iscc-lead }

## From upload to discovery

```text
 C2PA tool                ISCC C2PA Store                     ISCC hub           ISCC C2PA resolver
     |                           |                                |                       |
     |-- POST /v1/manifests ---->|                                |                       |
     |   application/c2pa        | 1. check with c2pa-rs          |                       |
     |                           | 2. index soft bindings         |                       |
     |                           | 3. store bytes, commit         |                       |
     |                           |-- 4. signed IsccNote --------->|                       |
     |                           |   gateway = manifest address   |                       |
     |                           |<-------------- ISCC-ID --------|                       |
     |<-- manifestId, manifestUrl, declaration                    |                       |
     |                           |                                |                       |
                                 |                                |   5. a client queries |
                                 |                                |   with the ISCC of    |
                                 |                                |   the content ------->|
                                 |                                |-- declaration, found  |
                                 |                                |   via iscc-search --->|
                                 |<------------- GET {manifest address} ------------------|
                                 |-- Manifest Store, byte for byte ---------------------->|
```

### 1. Check

The body of `POST /v1/manifests` must be sent as `application/c2pa`. Uploaded bytes are untrusted; only c2pa-rs
(through c2pa-python) parses them. The store accepts a Manifest Store when:

- c2pa-rs can read it and it has an active manifest;
- the label of the active manifest, which becomes the manifest ID, is printable and at most 255 characters long;
- the claim signature of the active manifest verifies (`claimSignature.validated`);
- the active manifest has no validation failure apart from the tolerated ones.

| Tolerated failure                                      | Why                                                                |
| ------------------------------------------------------ | ------------------------------------------------------------------ |
| Hard-binding mismatches (`dataHash`, `boxesHash`, ...) | The asset is not uploaded, so its hard binding cannot be checked   |
| `signingCredential.untrusted`                          | Recorded as untrusted, not refused; the client decides about trust |

Any other failure, a tampered soft-binding assertion among them, refuses the upload with `400` and the C2PA
validation status code of the first failure in `code`. An expired signing credential without a trusted timestamp
is such a failure (`signingCredential.expired`). Signing credentials are checked against the C2PA trust list that
ships with the store.

c2pa-rs runs without network access: no host is allowed, so checking an upload never makes it fetch anything an
uploader names. CAWG identity assertions are not evaluated (no `cawg.*` validation codes); they stay in the stored
bytes for the client to judge, and they neither refuse nor admit an upload.

The same bytes uploaded again return the stored result without a second check or declaration. Manifest IDs are
first come, first served: a manifest ID that is already taken by other bytes, or that belonged to a deleted
manifest, is refused with `409`. The store cannot tell a genuine manifest from one that took its label first. An
operator resolves such squatting by deleting the squatter and releasing its ID
([Operations](operations.md#operator-dashboard)); the genuine Manifest Store can then be uploaded.

### 2. Index

The store reads every soft-binding assertion of the active manifest (`c2pa.soft-binding`, also with an instance
suffix) and keeps each block whose algorithm is on the
[C2PA Soft Binding Algorithm List](https://github.com/c2pa-org/softbinding-algorithm-list) and whose value is at
most 3072 bytes, as an exact-match entry. Values of `io.iscc.v0` blocks are also decoded, once each, as ISCC-SEQ
([IEP-0020](https://ieps.iscc.codes/iep-0020/)) into searchable ISCC-UNITs: 256-bit Version 0 Semantic-,
Content-, Data- and Instance-Codes. Meta-Codes, shorter units and units of a SubType the store does not know are
not indexed, as IEP-0020 requires. An `io.iscc.v0` block without any searchable unit is not indexed at all, because
a query with such a value is refused.

Indexing has fixed limits per manifest. A Manifest Store with more than 64 such blocks, more than 8 of them
`io.iscc.v0`, or more than 32 distinct searchable ISCC-UNITs in its `io.iscc.v0` blocks is refused with `400` and
the code `manifest.tooManyBindings`.

A block with a `scope` (a region or time span of the asset) is indexed and found like any other, but only blocks
without a scope, which describe the whole asset, supply the units the store declares.

### 3. Store

The bytes are written first, as a file named by their SHA-256 under the data directory. Then the database record,
the bindings, the ISCC-UNITs and the initial declaration state are written in one transaction; if that fails, the
file is removed again. From then on the manifest address serves the bytes unchanged:

```text
https://c2pa-store-test.iscc.io/v1/manifests/urn%3Ac2pa%3AF9168C5E-CEB2-4FAA-B6BF-329BF39FA1E4
```

The manifest ID is percent-encoded in the address; the route accepts it encoded or not. An ID that is not
printable or longer than 255 characters cannot be stored and answers `404`.

### 4. Declare

After the commit, the store sends one signed IsccNote to its ISCC hub, with the manifest address as gateway URL,
and waits for the answer before it replies to the upload. A slow or failing hub never loses a manifest: the
declaration stays `pending` and a background loop retries it. See [Declarations](declarations.md) for the note,
the statuses and the identity that signs it.

### 5. Discovery

An [ISCC C2PA resolver](https://c2pa-resolver.iscc.codes/) that receives a query with the ISCC of the content finds
the declaration on the IDP, recognises its gateway URL as a manifest address, and fetches the Manifest Store from
the store. The store hosts nothing special for this: the manifest address is an ordinary route of its Soft Binding
Resolution API.

## Queries on the store

`/v1/matches/byBinding` searches the manifests of this store only.

1. **Decode.** The algorithm must be on the C2PA Soft Binding Algorithm List. The value is base64, standard or
    URL-safe, padded or not. Every space counts as a `+` that arrived unencoded, also at the start or end of the
    value; tabs and line breaks around the value are ignored. Values above 4096 characters are refused.
1. **Exact match.** Every stored block with the same algorithm and the same value bytes scores 100.
1. **ISCC similarity.** For `io.iscc.v0`, every searchable query unit is compared with the stored units of the
    same type (MainType, SubType and Version): Instance-Codes by equality, the others by Hamming distance of their
    256-bit bodies. A value with more than 32 searchable units is refused with `400`.
1. **Rank.** Each manifest keeps its best score. Similarity scores below the threshold (75 by default, the lowest
    score iscc-c2pa-resolver reports) are dropped. The 1000 best-scored manifests are ordered by score, then
    manifests with a trusted signing credential first, then by upload time, earlier first. Deleted manifests never
    match.

Matches carry no `endpoint`: as the C2PA Soft Binding Resolution API defines, the manifest is then fetched from the
same API with `GET /v1/manifests/{manifestId}`.

## Similarity score

The store uses the same score rule as iscc-c2pa-resolver, so a manifest gets the same score in both.

| Match                                                  | Score                                 |
| ------------------------------------------------------ | ------------------------------------- |
| Equal value for the queried algorithm                  | 100                                   |
| Equal Instance-Code: the same bytes as the source      | 100                                   |
| Otherwise, best Semantic-, Content- or Data-Code match | 100 × similarity, rounded, at most 99 |
| Meta-Code                                              | never contributes                     |

Similarity is `1 - Hamming distance / 256`. A score is a shortlisting signal, not a verdict: similar ISCC-UNITs
make a manifest a candidate, and the client confirms it, for example by comparing the claim thumbnail with the
asset, and validates the manifest as usual.

## Trust

Uploads need no account, and a stored manifest says nothing about who uploaded it. The store records whether the
signing credential is on the C2PA trust list and ranks such manifests first among equal scores, but never vouches
for the content. The store's
own signature on a declaration says only that the store hosts this manifest at this address; clients decide trust
by validating the recovered manifest, as for any C2PA Manifest.
