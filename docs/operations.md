---
title: Operations
description: Configure, run, back up and upgrade the ISCC C2PA Store
icon: lucide/server
---

# Operations

The store ships as a container image, `ghcr.io/iscc/iscc-c2pa-store`. One image serves mainnet and testnet;
environment variables select the public address, the ISCC hub and the limits. All state lives in one volume.
{ .iscc-lead }

## Run

```bash
docker run -d --name c2pa-store -p 127.0.0.1:8000:8000 \
    -v c2pa-store-data:/data \
    -e DJANGO_SECRET_KEY=<long random string> \
    -e ISCC_C2PA_STORE_URL=https://c2pa-store-test.iscc.io \
    -e ISCC_C2PA_STORE_SECKEY=<secret key from iscc-crypto keygen> \
    -e ISCC_C2PA_STORE_HUB_URL=https://staging.iscc.id \
    -e ISCC_C2PA_STORE_HUB_ID=1 \
    -e ISCC_C2PA_STORE_OPERATOR_TOKEN_SHA256=<digest from scripts/operator_token.py> \
    -e ISCC_C2PA_STORE_NUM_PROXIES=1 \
    ghcr.io/iscc/iscc-c2pa-store:main
```

The image runs as the unprivileged user `store` (uid 10001), listens on port 8000 and reports container health from
`GET /healthz`, which answers when the database does. A bind mount for `/data` must be writable by uid 10001.

## What the container runs

`docker/entrypoint.sh` does three things:

1. `python manage.py migrate --noinput`: applies database migrations on every start, so an upgrade needs no
    separate step.
1. A background loop that sleeps `ISCC_C2PA_STORE_RETRY_SECONDS` (120 by default) and then runs
    `python manage.py retry_declarations`, which retries pending declarations and unfinished withdrawals
    ([Declarations](declarations.md#retries)). A failing round is logged and the loop goes on; a value that `sleep`
    does not accept makes it wait 120 seconds instead.
1. gunicorn in the foreground, configured by `GUNICORN_CMD_ARGS`. The image sets it to
    `--bind 0.0.0.0:8000 --workers 1 --threads 8 --timeout 30 --access-logfile -`: port 8000, one worker process
    with 8 threads, a 30 second timeout, access log on stdout. Forwarded headers are read by the store itself, not
    by gunicorn ([Behind a reverse proxy](#behind-a-reverse-proxy)).

The single worker is deliberate. Rate limit counters live in the memory of the process (Django's local memory
cache), so more worker processes would each count separately and multiply the limits. Threads keep uploads and
fetches concurrent; SQLite runs in WAL mode with a 5 second busy timeout, which also lets the retry loop write next
to the server. Setting `GUNICORN_CMD_ARGS` replaces all of these defaults, so repeat the ones you keep, above all
`--bind 0.0.0.0:8000`, which the health check and the published port need. Run one container per instance:
replicas would also count limits separately and run their own retry loops.

## Configuration

`DJANGO_SECRET_KEY` and `ISCC_C2PA_STORE_URL` are required; the store does not start without them.
[`.env.example`](https://github.com/iscc/iscc-c2pa-store/blob/main/.env.example) is a template for local
development.

| Variable                                | Default                                      | Meaning                                                                                               |
| --------------------------------------- | -------------------------------------------- | ----------------------------------------------------------------------------------------------------- |
| `DJANGO_SECRET_KEY`                     | required                                     | Django secret; also keys the hash that groups uploads by client address                               |
| `ISCC_C2PA_STORE_URL`                   | required                                     | Public base URL; manifest addresses, the did:web identifier and the allowed host derive from it       |
| `DJANGO_DEBUG`                          | `false`                                      | Django debug mode; never on a public instance                                                         |
| `DJANGO_ALLOWED_HOSTS`                  | host of `ISCC_C2PA_STORE_URL`                | Comma-separated host names the store answers to; `localhost` and `127.0.0.1` are always added         |
| `ISCC_C2PA_STORE_DATA_DIR`              | `/data` in the image, `./data` in a checkout | SQLite database and manifest files                                                                    |
| `DATABASE_URL`                          | SQLite at `<data dir>/store.sqlite3`         | Database URL, for example `postgres://user:password@host:5432/store`                                  |
| `ISCC_C2PA_STORE_TRUST_ANCHORS`         | the bundled C2PA trust list                  | PEM file of trust anchors for signing credentials                                                     |
| `ISCC_C2PA_STORE_SECKEY`                | empty                                        | Ed25519 secret key of the store's declarations (multibase, from `iscc-crypto keygen`)                 |
| `ISCC_C2PA_STORE_HUB_URL`               | empty                                        | ISCC hub; with the secret key it enables declarations                                                 |
| `ISCC_C2PA_STORE_HUB_ID`                | `1`                                          | HUB-ID of that hub, carried in the nonces; each declaration keeps the hub and HUB-ID it was made with |
| `ISCC_C2PA_STORE_HUB_TIMEOUT`           | `5.0`                                        | Seconds per hub request                                                                               |
| `ISCC_C2PA_STORE_DECLARE_DEFAULT`       | `true`                                       | Whether uploads without a `declare` parameter are declared                                            |
| `ISCC_C2PA_STORE_RETRY_SECONDS`         | `120`                                        | Pause of the retry loop between rounds (container entrypoint)                                         |
| `ISCC_C2PA_STORE_OPERATOR_TOKEN_SHA256` | empty: deletes disabled                      | SHA-256 hex digest of the operator token for `DELETE /v1/manifests/{manifestId}`                      |
| `ISCC_C2PA_STORE_RATE_WRITE`            | `10/m`                                       | Uploads per client and period (`s`, `m`, `h` or `d`)                                                  |
| `ISCC_C2PA_STORE_RATE_READ`             | `120/m`                                      | Fetches and queries per client and period, counted apart from uploads                                 |
| `ISCC_C2PA_STORE_RATE_LOGIN`            | `10/m`                                       | Operator login attempts (`POST /admin/login/`) per client and period                                  |
| `ISCC_C2PA_STORE_UNTHROTTLED`           | empty                                        | Comma-separated client addresses exempt from all rate limits, such as the operator's resolvers        |
| `ISCC_C2PA_STORE_MAX_UPLOAD`            | `2097152`                                    | Upload limit in bytes                                                                                 |
| `ISCC_C2PA_STORE_MATCH_THRESHOLD`       | `75`                                         | Lowest ISCC similarity score a query reports; 75 is the lowest that iscc-c2pa-resolver reports        |
| `ISCC_C2PA_STORE_NUM_PROXIES`           | `0`                                          | Reverse proxies in front of the store; above `0` it trusts `X-Forwarded-For` and `-Proto`             |
| `GUNICORN_CMD_ARGS`                     | see [above](#what-the-container-runs)        | gunicorn options (container); a value replaces all defaults                                           |

A client is its IPv4 address or, for IPv6, its /64 network, because one subscriber usually holds a whole /64; an
IPv4-mapped IPv6 address counts as its IPv4 address. A client over its limit gets `429` with `Retry-After`.

Set `ISCC_C2PA_STORE_UNTHROTTLED` to the public addresses of the resolvers you run, for example the ones behind
`c2pa.iscc.io` or `c2pa-test.iscc.io`. A resolver fetches manifests from the store for all its users, from one
address, and would otherwise run into the read limit and leave the store's manifests out of its results. The
entries are compared with the client address as the store sees it, so they only work with a correct
`ISCC_C2PA_STORE_NUM_PROXIES`.

`ISCC_C2PA_STORE_URL` is part of every declaration and manifest address. Choose it once: declarations cannot be
changed, and the addresses they carry must keep working.

## Behind a reverse proxy

Terminate TLS at the proxy and forward to port 8000 with the original `Host` header and `X-Forwarded-Proto`. Set
`ISCC_C2PA_STORE_NUM_PROXIES` to the number of proxies that append to `X-Forwarded-For` (1 behind a single Caddy).
With a value above `0`, Django reads both headers itself:

- the client address is the entry of `X-Forwarded-For` that the outermost of these proxies appended (counted from
    the right); the rate limits and the uploader hash use it;
- `X-Forwarded-Proto: https` marks a request as secure, which the secure cookies and the CSRF check of the
    operator dashboard need.

With `0`, the store ignores both headers: every request seems to come from the proxy, and all clients share one
rate limit.

Anyone who reaches port 8000 directly could set these headers, so publish it only to the proxy, as `127.0.0.1:8000`
in the example above. Let the proxy accept bodies up to `ISCC_C2PA_STORE_MAX_UPLOAD`. The store sends its own CORS
headers on the public routes; the proxy should not add more.

## Operator access

### Operator token

Deletes need a bearer token. The store keeps only its SHA-256 digest:

```bash
uv run python scripts/operator_token.py
```

The script prints the token, which you keep, and the line for `ISCC_C2PA_STORE_OPERATOR_TOKEN_SHA256`. Delete a
manifest with:

```bash
curl -X DELETE -H "Authorization: Bearer <token>" \
    "https://c2pa-store-test.iscc.io/v1/manifests/urn:c2pa:F9168C5E-CEB2-4FAA-B6BF-329BF39FA1E4"
```

### Declaration key

Create one key per instance and keep the secret key secret:

```bash
uvx iscc-crypto keygen
```

It prints `public_key` and `secret_key` as JSON; set `secret_key` as `ISCC_C2PA_STORE_SECKEY`. The store publishes
the public key itself at `/.well-known/did.json` under `did:web:<host of ISCC_C2PA_STORE_URL>`. A hub that verifies
controllers, as `iscc.id` does, fetches that document, so the store must be reachable at its public address before
its first declaration there. Keep the key for the life of the instance: withdrawing a declaration on delete needs
the key that made it.

### Operator dashboard

The dashboard at `/admin/` shows the counts of manifests and bindings, the declarations of live manifests by
status, and the stored manifests with their bindings, ISCC units and declaration. It has three actions:

- **Delete manifests and withdraw their declarations**, like `DELETE /v1/manifests/{manifestId}`.
- **Release the IDs of deleted manifests for new uploads** removes the tombstones of the selected deleted
    manifests whose declaration is not `declared`, so that their manifest IDs and bytes can be uploaded again
    ([Releasing a manifest ID](declarations.md#releasing-a-manifest-id)). Like deleting, it needs the delete
    permission on manifests.
- **Retry declarations that need attention**, on the Declarations page, sends the selected pending or failed
    declarations to the hub and withdraws those still declared for a deleted manifest.

Manifest IDs are first come, first served, and anyone can sign a Manifest Store under a label they copied from a
published asset. When someone took the ID of a genuine manifest, delete the squatter, release its ID, and ask the
owner to upload the genuine Manifest Store again.

Create the first account in the running container:

```bash
docker exec -it c2pa-store python manage.py createsuperuser
```

Passwords need at least 12 characters. Login attempts are limited per client (`ISCC_C2PA_STORE_RATE_LOGIN`); above
the limit the login form answers `429`.

## Backups

Everything lives in `/data`: the SQLite database `store.sqlite3` (with its `-wal` and `-shm` files while running)
and the Manifest Stores under `media/manifests/`, one file per upload named by its SHA-256. For a consistent
copy while the store runs, back up the database with SQLite's backup API, then the files:

```bash
docker exec c2pa-store python -c "import sqlite3; sqlite3.connect('/data/store.sqlite3').backup(sqlite3.connect('/data/backup.sqlite3'))"
```

Then copy `backup.sqlite3` and `media/` off the volume. Files are only added and removed, never changed, so a file
copied after the database snapshot does no harm. Alternatively stop the container and copy the whole volume. Keep
`DJANGO_SECRET_KEY`, `ISCC_C2PA_STORE_SECKEY` and the operator token with the backup. After a restore, a manifest
whose file is missing answers `404` and logs an error naming the file.

## Images and upgrades

| Tag                 | Published when                                                                   |
| ------------------- | -------------------------------------------------------------------------------- |
| `main`, `sha-<sha>` | Every push to `main` that passes all checks; the image CI tested is pushed as is |
| `X.Y.Z`, `latest`   | A GitHub Release `vX.Y.Z`; the tested `sha-<sha>` image is retagged, not rebuilt |

Testnet follows `main`. Mainnet runs a release tag. To upgrade, back up `/data`, pull the new tag and recreate the
container with the same volume and environment; migrations run on start.

## Security

- Uploaded bytes are untrusted and reach c2pa-rs only. Request bodies are refused with `413` from their declared
    length before anything reads them: uploads above `ISCC_C2PA_STORE_MAX_UPLOAD`, other bodies above 64 KB. A
    `Content-Length` that is not plain ASCII digits is refused with `400`.
- The store never fetches URLs from requests or manifests, and c2pa-rs runs without network access. Its only
    outbound HTTP connections go to the configured hub.
- Client addresses are not stored: uploads keep a keyed hash of the address, enough to group them by client.
- The operator token is compared by digest in constant time. `DELETE` is not allowed cross-origin. Operator logins
    are rate limited.
- Uploads need no account. Their rate limit per client, separate from the one for reads, and the per-manifest
    indexing limits (64 soft-binding blocks, 8 of them `io.iscc.v0`, 32 ISCC-UNITs) bound what one client can make
    the store do. Restrict `POST /v1/manifests` at the proxy if an instance must not take anonymous uploads.
