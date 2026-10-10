---
title: ISCC C2PA Store
description: C2PA Manifest Repository with a Soft Binding Resolution API that declares ISCC soft bindings on the ISCC Discovery Protocol
icon: lucide/house
hide:
  - toc
---

# ISCC C2PA Store

!!! warning "Experimental"

    This beta MVP and its documentation are provided as is, without warranty of any kind. The API, the declaration behaviour and the public instances may change in breaking ways before version 1.0.

## A home for Content Credentials that outlives the file

A C2PA Manifest embedded in a file is lost when a platform strips metadata or re-encodes the file. The ISCC C2PA
Store keeps a copy of the C2PA Manifest Store, finds it again by soft binding, and declares its ISCC soft binding
on the ISCC Discovery Protocol, so that anyone who has only the content can find the manifest through any ISCC
C2PA resolver.
{ .iscc-lead }

[How it works](how-it-works.md){ .iscc-btn .iscc-btn--primary }
[API reference](api.md){ .iscc-btn }

*Open source · C2PA Soft Binding Resolution API, upcoming C2PA 2.5 · maintained by the [ISCC Foundation](https://iscc.io)*

## What it does

- **Stores** C2PA Manifest Stores sent to `POST /v1/manifests`, after c2pa-rs has read them and verified the claim
    signature of the active manifest.
- **Serves** each one byte for byte at its manifest address, `GET /v1/manifests/{manifestId}`.
- **Finds** them by soft binding: exact match for every algorithm on the C2PA Soft Binding Algorithm List, and
    similarity search for the ISCC fingerprint `io.iscc.v0`.
- **Declares** the `io.iscc.v0` soft binding of each upload on an ISCC hub, signed with the store's own did:web key,
    with the manifest address as gateway URL.

## Find your path

<div class="iscc-cards" markdown>

<div class="iscc-card" markdown>

### C2PA tools

Publish a Manifest Store with one request and get back its address and the state of its ISCC declaration.

[Use the API →](api.md)

</div>

<div class="iscc-card" markdown>

### ISCC declarers

See what the store signs on the ISCC Discovery Protocol, and how to declare a manifest yourself.

[Declarations →](declarations.md)

</div>

<div class="iscc-card" markdown>

### Operators

Run your own store for mainnet or testnet from the container image.

[Deploy →](operations.md)

</div>

</div>

## Public instances

| Network | Store                             | ISCC hub                  | Resolver                    |
| ------- | --------------------------------- | ------------------------- | --------------------------- |
| Mainnet | `https://c2pa-store.iscc.io`      | `https://iscc.id`         | `https://c2pa.iscc.io`      |
| Testnet | `https://c2pa-store-test.iscc.io` | `https://staging.iscc.id` | `https://c2pa-test.iscc.io` |

Each instance serves an interactive API reference at `/docs`, its discovery document at
`/.well-known/c2pa-soft-binding-resolution` and its declaration identity at `/.well-known/did.json`. The
[conformance page](conformance.md) lists where the store departs from the C2PA Soft Binding Resolution API.
