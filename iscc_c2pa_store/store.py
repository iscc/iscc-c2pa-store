"""The repository: store, fetch, delete and find C2PA Manifest Stores, and keep their ISCC declarations current.

Framework-free service functions over the Django models; `api.py` maps them to HTTP. Uploads are checked by
`manifest.read`, deduplicated by SHA-256, indexed by soft binding and declared after the database commit, so a slow
or failing hub never loses a manifest: its declaration stays `pending` for `retry_declarations`.
"""

import functools
import hashlib
import hmac
import json
import logging
from collections.abc import Iterable  # noqa: F401 (type comments)
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx2
import iscc_crypto as icr
from django.conf import settings
from django.core.cache import cache
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import IntegrityError, transaction
from django.db.models import Count, F, Q

from iscc_c2pa_store import binding, declare, manifest, similarity
from iscc_c2pa_store.models import Binding, Declaration, IsccUnit, Manifest

log = logging.getLogger(__name__)
Status = Declaration.Status
MAX_RESULTS = 100
MAX_CANDIDATES = 1000  # best-scored manifests a query ranks
MAX_BINDINGS = 64  # indexable soft-binding blocks per manifest
MAX_ISCC_BINDINGS = 8  # io.iscc.v0 blocks per manifest (each one is decoded)
MAX_UNITS = 32  # distinct searchable ISCC units per manifest or query
STALE_PENDING = timedelta(minutes=15)  # pending this long after the upload degrades the service status
LEASE = timedelta(seconds=60)  # the retry loop leaves declarations alone this long after their last change
MAX_BACKOFF = timedelta(hours=1)  # longest wait between two retries of one declaration
GIVE_UP = timedelta(days=7)  # a declaration still pending this long after the upload fails
STATISTICS_TTL = 30  # seconds the public page reuses the counts


class NotFoundError(LookupError):
    """No manifest with this ID was ever stored."""


class GoneError(LookupError):
    """The manifest was stored and then deleted."""


class ConflictError(ValueError):
    """The manifest ID is taken by different bytes, or was deleted."""


@dataclass(frozen=True)
class Index:
    """What the store indexes of a manifest: its bindings, their ISCC units, and the units it may declare."""

    bindings: list[manifest.SoftBinding]
    units: list[binding.Unit]  # of every io.iscc.v0 block, deduplicated
    declarable: list[binding.Unit]  # of the blocks that cover the whole asset (no scope), deduplicated


@dataclass(frozen=True)
class Match:
    """A stored manifest that matches a query."""

    manifest_id: str
    score: int
    iscc_id: str
    uploaded_at: datetime
    trusted: bool


@functools.cache
def algorithms():
    # type: () -> dict[str, str]
    """The C2PA Soft Binding Algorithm List as `alg -> type` (`fingerprint` or `watermark`)."""
    document = json.loads((manifest.VENDOR / "softbinding-algorithm-list.json").read_text(encoding="utf-8"))
    return {entry["alg"]: entry["type"] for entry in document}


def uploader_key(address):
    # type: (str) -> str
    """A keyed hash of a client address: enough to group uploads by client without storing the address."""
    return hmac.new(settings.SECRET_KEY.encode(), address.encode(), hashlib.sha256).hexdigest()[:32]


def manifest_url(manifest_id):
    # type: (str) -> str
    """The public address of a stored manifest, which is also its ISCC gateway URL."""
    return declare.gateway_url(settings.ISCC_C2PA_STORE_URL, manifest_id)


def declares():
    # type: () -> bool
    """Tell whether this deployment declares: it needs both a secret key and a hub."""
    return bool(settings.ISCC_C2PA_STORE_SECKEY and settings.ISCC_C2PA_STORE_HUB_URL)


def keypair():
    # type: () -> icr.KeyPair | None
    """The store's declaration key bound to its did:web identifier, or None when the deployment does not declare."""
    if not declares():
        return None
    return icr.key_from_secret(settings.ISCC_C2PA_STORE_SECKEY, controller=settings.ISCC_C2PA_STORE_CONTROLLER)


def hub_client():
    # type: () -> httpx2.Client
    """An HTTP client for the configured hub."""
    return httpx2.Client(
        timeout=settings.ISCC_C2PA_STORE_HUB_TIMEOUT,
        transport=settings.ISCC_C2PA_STORE_HUB_TRANSPORT,
        headers={"User-Agent": "iscc-c2pa-store (+https://github.com/iscc/iscc-c2pa-store)"},
    )


def ingest(data, declare_it, uploader):
    # type: (bytes, bool, str) -> Manifest
    """Store a Manifest Store and declare its ISCC soft binding; the same bytes again return the stored record.

    :raises manifest.InvalidManifestError: if the bytes are not an acceptable C2PA Manifest Store.
    :raises ConflictError: if the manifest ID is taken by other bytes or was deleted.
    """
    sha256 = hashlib.sha256(data).hexdigest()
    known = existing(sha256)
    if known:
        return known
    info = manifest.read(data, settings.ISCC_C2PA_STORE_TRUST_ANCHORS)
    index = plan_index(info.bindings)
    if Manifest.objects.filter(pk=info.manifest_id).exists():
        raise ConflictError(f"Manifest ID {info.manifest_id} is taken")
    try:
        record = save(data, sha256, info, index, declare_it, uploader)
    except IntegrityError:  # a concurrent upload of the same bytes or ID won the race
        known = existing(sha256)
        if known:
            return known
        raise ConflictError(f"Manifest ID {info.manifest_id} is taken") from None
    if record.declaration.status == Status.PENDING:
        attempt(record.declaration)
    return record


def existing(sha256):
    # type: (str) -> Manifest | None
    """The stored record of these bytes, None if they are new.

    :raises ConflictError: if these bytes were stored and deleted.
    """
    record = Manifest.objects.select_related("declaration").filter(sha256=sha256).first()
    if record and record.deleted_at:
        raise ConflictError(f"Manifest {record.manifest_id} was deleted")
    return record


def save(data, sha256, info, index, declare_it, uploader):
    # type: (bytes, str, manifest.ManifestInfo, Index, bool, str) -> Manifest
    """Write the bytes, then the records in one transaction; the bytes are removed again if the records fail."""
    name = default_storage.save(f"manifests/{sha256}.c2pa", ContentFile(data))
    try:
        return save_records(name, len(data), sha256, info, index, declare_it, uploader)
    except BaseException:
        default_storage.delete(name)
        raise


@transaction.atomic
def save_records(name, size, sha256, info, index, declare_it, uploader):
    # type: (str, int, str, manifest.ManifestInfo, Index, bool, str) -> Manifest
    """Write the manifest record, its bindings, its ISCC units and its declaration state."""
    record = Manifest(
        manifest_id=info.manifest_id,
        sha256=sha256,
        size=size,
        title=info.title[:255],
        media_type=info.media_type[:255],
        claim_generator=info.claim_generator[:255],
        signer=info.signer[:255],
        signed_at=info.signed_at[:64],
        trusted=info.trusted,
        validation_state=info.validation_state[:32],
        failures=list(info.failures),
        uploader=uploader,
    )
    record.blob.name = name
    record.save(force_insert=True)
    Binding.objects.bulk_create(
        Binding(manifest=record, alg=b.alg, value=b.value, scope=b.scope) for b in index.bindings
    )
    IsccUnit.objects.bulk_create(IsccUnit(manifest=record, unit_type=u.unit_type, body=u.body) for u in index.units)
    record.declaration = Declaration.objects.create(manifest=record, **declaration_plan(index.declarable, declare_it))
    return record


def plan_index(bindings):
    # type: (tuple[manifest.SoftBinding, ...]) -> Index
    """Choose what a query can find: bindings with an algorithm on the C2PA list and a value within the query limit,
    for `io.iscc.v0` only valid ISCC-SEQs, each decoded once.

    :raises manifest.InvalidManifestError: if the manifest carries more bindings or ISCC units than the store indexes.
    """
    registered = algorithms()
    candidates = [b for b in bindings if b.alg in registered and len(b.value) <= binding.MAX_VALUE_BYTES]
    iscc = [b for b in candidates if b.alg == binding.ISCC_ALG]
    if len(candidates) > MAX_BINDINGS or len(iscc) > MAX_ISCC_BINDINGS:
        raise too_many(f"at most {MAX_BINDINGS} soft-binding blocks, {MAX_ISCC_BINDINGS} of them io.iscc.v0")
    decoded = {id(block): iscc_block_units(block) for block in iscc}
    kept = [b for b in candidates if b.alg != binding.ISCC_ALG or decoded[id(b)]]
    units = unique(unit for block in iscc for unit in decoded[id(block)])
    if len(units) > MAX_UNITS:
        raise too_many(f"at most {MAX_UNITS} distinct ISCC units")
    whole = unique(unit for block in iscc if not block.scope for unit in decoded[id(block)])
    return Index(bindings=kept, units=units, declarable=whole)


def iscc_block_units(block):
    # type: (manifest.SoftBinding) -> list[binding.Unit]
    """The searchable units of an `io.iscc.v0` block; empty if its value is not a valid ISCC-SEQ."""
    try:
        return binding.decode_units(block.value)
    except ValueError:
        return []


def unique(units):
    # type: (Iterable[binding.Unit]) -> list[binding.Unit]
    """Units without repeats of the same type and body, in first-seen order."""
    found = {}  # type: dict[tuple[str, bytes], binding.Unit]
    for unit in units:
        found.setdefault((unit.unit_type, unit.body), unit)
    return list(found.values())


def too_many(limit):
    # type: (str) -> manifest.InvalidManifestError
    """The refusal of a manifest that carries more than the store indexes."""
    return manifest.InvalidManifestError("manifest.tooManyBindings", f"This store indexes {limit} per manifest")


def declaration_plan(units, declare_it):
    # type: (list[binding.Unit], bool) -> dict
    """The initial declaration fields: disabled without a key, skipped without declarable units or on request."""
    if not declares():
        return {"status": Status.DISABLED}
    declarable = declare.declarable_units(units)
    if not (declare_it and declarable):
        return {"status": Status.SKIPPED}
    return {
        "status": Status.PENDING,
        "hub": settings.ISCC_C2PA_STORE_HUB_URL,
        "hub_id": settings.ISCC_C2PA_STORE_HUB_ID,
        "units": declarable,
    }


def claim(declaration):
    # type: (Declaration) -> bool
    """Take a pending declaration for one hub request, atomically across threads and processes.

    Counts the attempt and renews the lease; False if another request took it first or it is no longer pending.
    """
    taken = Declaration.objects.filter(pk=declaration.pk, status=Status.PENDING, attempts=declaration.attempts).update(
        attempts=F("attempts") + 1, updated_at=timestamp()
    )
    return taken == 1


def attempt(declaration):
    # type: (Declaration) -> Declaration
    """Send a pending declaration to the hub once, if no other request is sending it, and record the outcome.

    A declaration the hub accepted is recorded as declared even if its manifest was deleted meanwhile, so that the
    retry loop withdraws it. A refusal or failure only updates a declaration that is still pending.
    """
    key = keypair()
    if key is None or not claim(declaration):
        return declaration
    gateway = manifest_url(declaration.manifest_id)
    note = declare.build_note(declaration.units, gateway, key, declaration.hub_id, timestamp())
    with hub_client() as client:
        outcome = declare.submit(client, declaration.hub, note)
    fields = {"note": note, "iscc_code": note["iscc_code"], "last_error": outcome.error, "updated_at": timestamp()}
    rows = Declaration.objects.filter(pk=declaration.pk)
    if outcome.ok:  # attempts count again from zero for withdrawing it later
        rows.update(status=Status.DECLARED, iscc_id=outcome.iscc_id, declared_at=timestamp(), attempts=0, **fields)
    else:
        rows.filter(status=Status.PENDING).update(**fields, **({"status": Status.FAILED} if outcome.final else {}))
    declaration.refresh_from_db()
    log.info("Declaration of %s: %s %s", declaration.manifest_id, declaration.status, outcome.error)
    return declaration


def requeue(declaration):
    # type: (Declaration) -> Declaration
    """Make a failed declaration pending again, for a retry an operator asked for."""
    Declaration.objects.filter(pk=declaration.pk, status=Status.FAILED).update(
        status=Status.PENDING, updated_at=timestamp()
    )
    declaration.refresh_from_db()
    return declaration


def timestamp():
    # type: () -> datetime
    """The current time, timezone-aware."""
    return datetime.now(UTC)


def fetch(manifest_id):
    # type: (str) -> Manifest
    """The record of a stored manifest, for serving its bytes.

    :raises NotFoundError: if it was never stored.
    :raises GoneError: if it was deleted.
    """
    record = Manifest.objects.filter(pk=manifest_id).first() if is_manifest_id(manifest_id) else None
    if record is None:
        raise NotFoundError(manifest_id)
    if record.deleted_at:
        raise GoneError(manifest_id)
    return record


def is_manifest_id(text):
    # type: (str) -> bool
    """Tell whether a text can be a stored manifest ID: printable and at most MAX_ID_LENGTH characters."""
    return text.isprintable() and len(text) <= manifest.MAX_ID_LENGTH


def read_bytes(record):
    # type: (Manifest) -> bytes
    """The stored Manifest Store bytes of a record.

    :raises GoneError: if the manifest was deleted after the record was read.
    :raises NotFoundError: if the file of a live record is missing (logged as an error).
    """
    try:
        with record.blob.open("rb") as blob:
            return blob.read()
    except FileNotFoundError:
        fetch(record.manifest_id)  # raises GoneError if a delete came in between
        log.error("The file of manifest %s is missing: %s", record.manifest_id, record.blob.name)
        raise NotFoundError(record.manifest_id) from None


def delete(manifest_id):
    # type: (str) -> Manifest
    """Delete a manifest's bytes and index entries, keep a tombstone, and withdraw the store's declaration.

    :raises NotFoundError: if it was never stored.
    :raises GoneError: if it was deleted before.
    """
    record = fetch(manifest_id)
    with transaction.atomic():
        record.bindings.all().delete()
        record.units.all().delete()
        record.deleted_at = timestamp()
        record.save(update_fields=["deleted_at"])
        # A pending or failed declaration has no ISCC-ID to withdraw. If a request of it is in flight and the hub
        # accepts it, attempt() records it as declared and the retry loop withdraws it. A note the hub accepted while
        # its answer was lost (timeout) stays on the hub, pointing to an address that answers 410.
        Declaration.objects.filter(manifest=record, status__in=[Status.PENDING, Status.FAILED]).update(
            status=Status.WITHDRAWN, updated_at=timestamp()
        )
    record.blob.delete(save=True)
    declaration = Declaration.objects.filter(manifest=record, status=Status.DECLARED).first()
    if declaration:
        withdraw(declaration)
    return record


def release(manifest_id):
    # type: (str) -> bool
    """Forget a deleted manifest, so that its ID and bytes can be uploaded again.

    False if it is not deleted, its declaration is still on the hub, or the declaration changed within LEASE.
    """
    tombstone = (
        Manifest.objects.filter(pk=manifest_id, deleted_at__isnull=False)
        .exclude(declaration__status=Status.DECLARED)
        .exclude(declaration__updated_at__gte=timestamp() - LEASE)  # a declaration request may still be in flight
        .first()
    )
    if tombstone is None:
        return False
    default_storage.delete(f"manifests/{tombstone.sha256}.c2pa")  # left behind if deleting the bytes failed
    tombstone.delete()
    return True


def withdraw(declaration):
    # type: (Declaration) -> Declaration
    """Delete a declaration from the hub. On failure it stays `declared` with the error; retries pick it up."""
    key = keypair()
    if key is None:
        error, status = "Withdrawal needs the declaration key", Status.DECLARED
    else:
        note = declare.build_delete(declaration.iscc_id, key, declaration.hub_id, timestamp())
        with hub_client() as client:
            outcome = declare.withdraw(client, declaration.hub, note)
        error, status = outcome.error, Status.WITHDRAWN if outcome.ok else Status.DECLARED
    Declaration.objects.filter(pk=declaration.pk, status=Status.DECLARED).update(
        status=status, last_error=error, attempts=F("attempts") + 1, updated_at=timestamp()
    )
    declaration.refresh_from_db()
    return declaration


def retry_declarations():
    # type: () -> int
    """Retry pending declarations and unfinished withdrawals of deleted manifests; return how many were tried.

    A declaration is due once its backoff has passed since its last change (LEASE doubled per attempt, at most
    MAX_BACKOFF), so a request still in flight elsewhere is left alone. Declarations still pending GIVE_UP after the
    upload fail.
    """
    now = timestamp()
    Declaration.objects.filter(status=Status.PENDING, manifest__uploaded_at__lt=now - GIVE_UP).update(
        status=Status.FAILED, last_error=f"Gave up after {GIVE_UP.days} days", updated_at=now
    )
    open_work = Q(status=Status.PENDING, manifest__deleted_at=None) | Q(
        status=Status.DECLARED, manifest__deleted_at__isnull=False
    )
    due = [d for d in Declaration.objects.filter(open_work, updated_at__lt=now - LEASE) if is_due(d, now)]
    for declaration in due:
        (attempt if declaration.status == Status.PENDING else withdraw)(declaration)
    return len(due)


def is_due(declaration, now):
    # type: (Declaration, datetime) -> bool
    """Tell whether a declaration's backoff has passed: LEASE doubled per attempt, at most MAX_BACKOFF."""
    return declaration.updated_at <= now - min(LEASE * 2 ** min(declaration.attempts, 12), MAX_BACKOFF)


def query(alg, value, max_results):
    # type: (str, bytes, int) -> list[Match]
    """Find stored manifests by soft binding: exact value matches for any algorithm, and ISCC similarity.

    :raises ValueError: if the algorithm is not on the C2PA list, or an `io.iscc.v0` value is not a valid ISCC-SEQ
        or holds more than MAX_UNITS searchable units.
    """
    if alg not in algorithms():
        raise ValueError(f"Unknown soft binding algorithm: {alg}")
    scores = dict.fromkeys(
        Binding.objects.filter(alg=alg, value=value, manifest__deleted_at=None).values_list("manifest_id", flat=True),
        similarity.EQUAL,
    )
    if alg == binding.ISCC_ALG:
        units = binding.decode_units(value)
        if len(units) > MAX_UNITS:
            raise ValueError(f"Value holds more than {MAX_UNITS} searchable ISCC units")
        for manifest_id, score in iscc_scores(units).items():
            scores[manifest_id] = max(score, scores.get(manifest_id, 0))
    return ranked(scores, min(max_results, MAX_RESULTS))


def iscc_scores(units):
    # type: (list[binding.Unit]) -> dict[str, int]
    """Score every manifest with a unit of the same type as a query unit, keeping those at or above the threshold.

    The stored units of each type are read once, however many query units have that type.
    """
    best = {}  # type: dict[str, int]
    for unit_type in dict.fromkeys(unit.unit_type for unit in units):
        rows = list(
            IsccUnit.objects.filter(unit_type=unit_type, manifest__deleted_at=None).values_list("manifest_id", "body")
        )
        bodies = [bytes(body) for _, body in rows]
        for unit in (u for u in units if u.unit_type == unit_type):
            for manifest_id, score in unit_scores(unit, rows, bodies):
                best[manifest_id] = max(score, best.get(manifest_id, 0))
    threshold = settings.ISCC_C2PA_STORE_MATCH_THRESHOLD
    return {manifest_id: score for manifest_id, score in best.items() if score >= threshold}


def unit_scores(unit, rows, bodies):
    # type: (binding.Unit, list[tuple[str, bytes]], list[bytes]) -> list[tuple[str, int]]
    """Score stored units of one type against a query unit: by Hamming similarity, Instance-Codes by equality."""
    if unit.maintype in binding.SIMILARITY_TYPES:
        values = similarity.hamming_similarity(unit.body, bodies)
        return [(mid, similarity.percent(float(v))) for (mid, _), v in zip(rows, values, strict=True)]
    return [(mid, similarity.EQUAL) for (mid, _), body in zip(rows, bodies, strict=True) if body == unit.body]


def ranked(scores, limit):
    # type: (dict[str, int], int) -> list[Match]
    """Order scored manifests by score, then trusted signers first, then upload time (earlier first), and attach
    their ISCC-IDs. Only the MAX_CANDIDATES best scores are considered.
    """
    best = sorted(scores, key=lambda mid: -scores[mid])[:MAX_CANDIDATES]
    rows = Manifest.objects.filter(pk__in=best).values_list(
        "manifest_id", "uploaded_at", "trusted", "declaration__iscc_id"
    )
    matches = [Match(mid, scores[mid], iscc_id or "", uploaded, trusted) for mid, uploaded, trusted, iscc_id in rows]
    matches.sort(key=lambda m: (-m.score, not m.trusted, m.uploaded_at, m.manifest_id))
    return matches[:limit]


def supported_algorithms():
    # type: () -> dict[str, list[str]]
    """The algorithms the store answers queries for: those it has indexed plus `io.iscc.v0`, by C2PA list type."""
    registered = algorithms()
    indexed = set(Binding.objects.values_list("alg", flat=True).distinct()) | {binding.ISCC_ALG}
    return {
        kind: sorted(alg for alg in indexed if registered.get(alg) == kind) for kind in ("fingerprint", "watermark")
    }


def service_status():
    # type: () -> str
    """`degraded` while the declaration of a manifest uploaded more than STALE_PENDING ago is pending, else `ok`."""
    stale = Declaration.objects.filter(status=Status.PENDING, manifest__uploaded_at__lt=timestamp() - STALE_PENDING)
    return "degraded" if stale.exists() else "ok"


def statistics():
    # type: () -> dict
    """Counts for the operator dashboard: manifests, bindings per algorithm and declarations per status."""
    live = Manifest.objects.filter(deleted_at=None)
    return {
        "manifests": live.count(),
        "trusted": live.filter(trusted=True).count(),
        "deleted": Manifest.objects.exclude(deleted_at=None).count(),
        "bindings": dict(Binding.objects.values_list("alg").annotate(n=Count("id")).order_by("-n")),
        "declarations": dict(
            Declaration.objects.filter(manifest__deleted_at=None)
            .values_list("status")
            .annotate(n=Count("pk"))
            .order_by("-n")
        ),
    }


def cached_statistics():
    # type: () -> dict
    """The statistics, recomputed at most every STATISTICS_TTL seconds, for pages anyone can request."""
    return cache.get_or_set("iscc_c2pa_store:statistics", statistics, STATISTICS_TTL) or {}
