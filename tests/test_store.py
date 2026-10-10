"""Service layer tests: ingest and indexing, declarations, fetch, delete, release, retries, queries and the dashboard
figures of `store.py`.

Every Manifest Store is signed for the test by c2pa-rs (see `conftest.py`); declarations go to the fake hub.
"""

import base64
import hashlib
import hmac
import io
import json
import logging
import uuid
from collections.abc import Callable  # noqa: F401 (type comments)
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import c2pa
import httpx2
import iscc_crypto as icr
import pytest
from conftest import (
    C2PA_RS_MANIFEST,
    CHAIN,
    CONTENT,
    DATA_CODE,
    DIGITAL,
    INSTANCE,
    JPEG,
    META,
    SECKEY,
    VECTOR_1,
    FakeHub,
    encode,
    es256,
    flip,
    iscc_manifest,
    iscc_units,
    sign_manifest,
)
from django.core.cache import cache
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import OperationalError, connection
from django.test.utils import CaptureQueriesContext

from iscc_c2pa_store import binding, manifest, store
from iscc_c2pa_store.models import Binding, Declaration, IsccUnit, Manifest

TRUSTMARK = "com.adobe.trustmark.Q"
TIME_SPAN = {"timeSpan": {"start": 0, "end": 10}}  # a block scope that covers part of the asset
Status = Declaration.Status


def b64(raw):
    # type: (bytes) -> str
    """Base64 text of raw bytes, as a soft-binding block value for `sign_manifest`."""
    return base64.b64encode(raw).decode("ascii")


def signed(blocks=(), label=None, title="test.jpg"):
    # type: (list[tuple[dict, str]] | tuple, str | None, str) -> bytes
    """A Manifest Store with one `io.iscc.v0` soft-binding assertion of (scope, base64 value) blocks, if any, and
    optionally a fixed label, so that different stores share one manifest ID.
    """
    assertions = [
        {"label": "c2pa.actions", "data": {"actions": [{"action": "c2pa.created", "digitalSourceType": DIGITAL}]}}
    ]
    if blocks:
        data = {"alg": "io.iscc.v0", "blocks": [{"scope": scope, "value": value} for scope, value in blocks]}
        assertions.append({"label": "c2pa.soft-binding", "data": data})
    definition = {
        "claim_generator_info": [{"name": "iscc-c2pa-store-tests", "version": "1.0"}],
        "title": title,
        "format": "image/jpeg",
        "assertions": assertions,
    }
    if label:
        definition["label"] = label
    signer = c2pa.Signer.from_callback(es256, c2pa.C2paSigningAlg.ES256, CHAIN.pem, None)
    with c2pa.Builder(definition) as builder:
        return builder.sign(signer, "image/jpeg", io.BytesIO(JPEG.getvalue()), io.BytesIO())


def new_label():
    # type: () -> str
    """A fresh C2PA manifest label."""
    return f"urn:c2pa:{uuid.uuid4()}"


def ingest(data, declare_it=True):
    # type: (bytes, bool) -> Manifest
    """Ingest bytes for a fixed test uploader."""
    return store.ingest(data, declare_it, "uploader")


def seq(units):
    # type: (list[str]) -> bytes
    """The raw `io.iscc.v0` bytes of ISCC-UNITs, as a query value."""
    return base64.b64decode(encode(units))


def blobs(sha256):
    # type: (str) -> list[str]
    """The stored files named after the SHA-256 of a Manifest Store."""
    return [name for name in default_storage.listdir("manifests")[1] if name.startswith(sha256)]


def backdate(record, minutes):
    # type: (Manifest, int) -> None
    """Move the upload time of a stored manifest into the past."""
    Manifest.objects.filter(pk=record.pk).update(uploaded_at=datetime.now(UTC) - timedelta(minutes=minutes))


def expire():
    # type: () -> None
    """Move the last change of every declaration back past the longest backoff, so the retry loop takes them."""
    age(store.MAX_BACKOFF + timedelta(seconds=1))


def age(delta):
    # type: (timedelta) -> None
    """Move the last change of every declaration back by `delta` from now."""
    Declaration.objects.update(updated_at=datetime.now(UTC) - delta)


def nonce_hub(note):
    # type: (dict) -> int
    """The hub ID in the nonce of a signed note."""
    return icr.decode_node_id(note["nonce"])


def bare(manifest_id, trusted=False):
    # type: (str, bool) -> Manifest
    """A manifest record without bytes, for ranking tests that need many of them."""
    return Manifest(
        manifest_id=manifest_id,
        sha256=hashlib.sha256(manifest_id.encode()).hexdigest(),
        size=1,
        trusted=trusted,
        validation_state="Valid",
        uploader="uploader",
    )


def failing_declaration_update(execute, sql, params, many, context):
    # type: (Callable[..., object], str, object, bool, dict) -> object
    """A database that fails every UPDATE of a declaration, as a locked database would."""
    if sql.startswith('UPDATE "iscc_c2pa_store_declaration"'):
        raise OperationalError("database is locked")
    return execute(sql, params, many, context)


def created_without_json(request):
    # type: (httpx2.Request) -> httpx2.Response
    """A hub that answers a declaration with 201 and a body that is not JSON (e.g. a misconfigured proxy)."""
    return httpx2.Response(201, text="<html>Created</html>")


@dataclass
class DeletingHub:
    """A hub that sees the manifest deleted while the first request is in flight, then answers like `hub`."""

    hub: FakeHub
    manifest_id: str
    fired: bool = False

    def __call__(self, request):
        # type: (httpx2.Request) -> httpx2.Response
        """Delete the manifest once, then let the fake hub answer."""
        if not self.fired:
            self.fired = True
            store.delete(self.manifest_id)
        return self.hub(request)


@dataclass
class Race:
    """A competing upload that commits its row after `ingest` has checked the bytes and the ID, but before it saves.

    Used as a Django execute wrapper: the first savepoint opened during `ingest` is the one of `store.save`, so the
    competing `store.save` runs right before it, as a concurrent request would.
    """

    data: bytes
    winner: Manifest | None = None
    fired: bool = False

    def __call__(self, execute, sql, params, many, context):
        # type: (Callable[..., object], str, object, bool, dict) -> object
        """Save the competing bytes before the first savepoint, then run the statement."""
        if not self.fired and sql.startswith("SAVEPOINT"):
            self.fired = True
            info = manifest.read(self.data)
            sha256 = hashlib.sha256(self.data).hexdigest()
            self.winner = store.save(self.data, sha256, info, store.plan_index(info.bindings), False, "competitor")
        return execute(sql, params, many, context)


# ingest


@pytest.mark.django_db
def test_ingest_stores_record_bytes_and_summary():
    data = sign_manifest(title="photo.jpg")
    record = ingest(data)
    stored = Manifest.objects.get(pk=record.manifest_id)
    assert record.manifest_id == manifest.read(data).manifest_id
    assert stored.sha256 == hashlib.sha256(data).hexdigest()
    assert stored.blob.name == f"manifests/{stored.sha256}.c2pa"
    assert stored.size == len(data)
    assert stored.title == "photo.jpg"
    assert stored.claim_generator == "iscc-c2pa-store-tests/1.0"
    assert stored.signer == "ISCC Test Signer"
    assert stored.trusted is False
    assert manifest.UNTRUSTED in stored.failures
    assert stored.uploader == "uploader"
    assert stored.deleted_at is None
    assert store.read_bytes(stored) == data
    assert stored.declaration.status == Status.DISABLED


@pytest.mark.django_db
def test_ingest_c2pa_rs_fixture():
    record = ingest(C2PA_RS_MANIFEST)
    assert record.manifest_id == "contentauth:urn:uuid:b2b1f7fa-b119-4de1-9c0d-c97fbea3f2c3"
    assert record.title == "C.jpg"
    assert record.media_type == "image/jpeg"
    assert record.claim_generator == "make_test_images/0.33.1 c2pa-rs/0.33.1"
    assert record.signer == "C2PA Signer"
    assert record.signed_at == "2024-08-06T21:53:37+00:00"
    assert record.trusted is False
    assert not record.bindings.exists()
    assert record.declaration.status == Status.DISABLED
    assert store.read_bytes(store.fetch(record.manifest_id)) == C2PA_RS_MANIFEST


@pytest.mark.django_db
def test_ingest_trusted_with_trust_anchors(settings, trust_anchors):
    settings.ISCC_C2PA_STORE_TRUST_ANCHORS = trust_anchors
    record = ingest(sign_manifest())
    assert record.trusted is True
    assert manifest.UNTRUSTED not in record.failures


@pytest.mark.django_db
def test_ingest_rejects_invalid_bytes():
    with pytest.raises(manifest.InvalidManifestError):
        ingest(b"not a manifest store")
    assert not Manifest.objects.exists()


@pytest.mark.django_db
def test_ingest_is_idempotent(hub):
    data = iscc_manifest()
    first = ingest(data)
    second = ingest(data)
    assert second.manifest_id == first.manifest_id
    assert second.declaration.status == Status.DECLARED
    assert second.declaration.iscc_id == first.declaration.iscc_id
    assert Manifest.objects.count() == 1
    assert len(hub.requests) == 1
    assert len(blobs(first.sha256)) == 1


@pytest.mark.django_db
def test_ingest_conflict_on_reused_manifest_id():
    label = new_label()
    first, second = signed(label=label, title="a.jpg"), signed(label=label, title="b.jpg")
    assert first != second
    assert ingest(first).manifest_id == label
    with pytest.raises(store.ConflictError, match="is taken"):
        ingest(second)
    assert Manifest.objects.get().sha256 == hashlib.sha256(first).hexdigest()


@pytest.mark.django_db
def test_ingest_conflict_on_deleted_bytes():
    data = sign_manifest()
    store.delete(ingest(data).manifest_id)
    with pytest.raises(store.ConflictError, match="was deleted"):
        ingest(data)


@pytest.mark.django_db
def test_ingest_conflict_on_deleted_manifest_id():
    label = new_label()
    store.delete(ingest(signed(label=label, title="a.jpg")).manifest_id)
    with pytest.raises(store.ConflictError, match="is taken"):
        ingest(signed(label=label, title="b.jpg"))


@pytest.mark.django_db
def test_ingest_concurrent_same_bytes_returns_winner():
    data = sign_manifest()
    race = Race(data)
    with connection.execute_wrapper(race):
        record = ingest(data)
    assert race.winner is not None
    assert record.manifest_id == race.winner.manifest_id
    assert record.uploader == "competitor"
    assert Manifest.objects.count() == 1
    assert store.read_bytes(record) == data
    assert blobs(record.sha256) == [record.blob.name.removeprefix("manifests/")]


@pytest.mark.django_db
def test_ingest_concurrent_other_bytes_conflict():
    label = new_label()
    race = Race(signed(label=label, title="winner.jpg"))
    with connection.execute_wrapper(race), pytest.raises(store.ConflictError, match="is taken"):
        ingest(signed(label=label, title="loser.jpg"))
    assert Manifest.objects.get().title == "winner.jpg"


@pytest.mark.django_db
def test_ingest_concurrent_loser_leaves_no_blob():
    label = new_label()
    loser = signed(label=label, title="loser.jpg")
    with connection.execute_wrapper(Race(signed(label=label, title="winner.jpg"))), pytest.raises(store.ConflictError):
        ingest(loser)
    assert blobs(hashlib.sha256(loser).hexdigest()) == []


# bindings and ISCC units


@pytest.mark.django_db
def test_ingest_keeps_registered_bindings_up_to_max_size():
    limit = binding.MAX_VALUE_BYTES
    data = sign_manifest(
        [
            ("io.iscc.v0", encode([DATA_CODE, INSTANCE])),
            ("com.example.unregistered", b64(b"\x01\x02\x03")),
            (TRUSTMARK, b64(b"\x07" * (limit + 1))),
            ("com.adobe.icn.dense", b64(b"\x08" * limit)),
        ]
    )
    record = ingest(data)
    stored = {b.alg: bytes(b.value) for b in record.bindings.all()}
    assert stored == {"io.iscc.v0": seq([DATA_CODE, INSTANCE]), "com.adobe.icn.dense": b"\x08" * limit}
    assert Binding.objects.get(alg="io.iscc.v0").scope == {}


@pytest.mark.django_db
def test_ingest_deduplicates_iscc_units_and_drops_undecodable_values():
    data = sign_manifest(
        [
            ("io.iscc.v0", encode([DATA_CODE, INSTANCE])),
            ("io.iscc.v0", encode([CONTENT, DATA_CODE, INSTANCE])),
            ("io.iscc.v0", b64(b"junk")),
            ("io.iscc.v0", encode([META])),
        ]
    )
    record = ingest(data)
    assert sorted(bytes(b.value) for b in record.bindings.all()) == sorted(
        [seq([DATA_CODE, INSTANCE]), seq([CONTENT, DATA_CODE, INSTANCE])]
    )
    assert sorted(record.units.values_list("unit_type", flat=True)) == [
        "CONTENT_TEXT_V0",
        "DATA_NONE_V0",
        "INSTANCE_NONE_V0",
    ]


@pytest.mark.django_db
def test_ingest_indexes_scoped_blocks_but_declares_whole_asset_units(hub):
    units = iscc_units(b"scoped asset" * 50)
    record = ingest(signed([(TIME_SPAN, encode([CONTENT])), ({}, encode(units))]))
    assert sorted(record.units.values_list("unit_type", flat=True)) == [
        "CONTENT_TEXT_V0",
        "DATA_NONE_V0",
        "INSTANCE_NONE_V0",
    ]
    assert sorted((b.scope for b in record.bindings.all()), key=len) == [{}, TIME_SPAN]
    declaration = Declaration.objects.get(pk=record.pk)
    assert declaration.status == Status.DECLARED
    assert declaration.units == units
    assert [m.manifest_id for m in store.query("io.iscc.v0", seq([CONTENT]), 10)] == [record.manifest_id]


@pytest.mark.django_db
def test_ingest_refuses_too_many_bindings_before_the_id_check():
    label = new_label()
    ingest(signed(label=label, title="a.jpg"))
    blocks = [({}, encode([flip(DATA_CODE, bits)])) for bits in range(store.MAX_ISCC_BINDINGS + 1)]
    with pytest.raises(manifest.InvalidManifestError) as error:
        ingest(signed(blocks, label=label, title="b.jpg"))
    assert error.value.code == "manifest.tooManyBindings"
    assert Manifest.objects.count() == 1


def block(value, alg="io.iscc.v0", scope=None):
    # type: (bytes, str, dict | None) -> manifest.SoftBinding
    """A soft-binding block as `manifest.read` reports it."""
    return manifest.SoftBinding(alg, value, scope or {})


def distinct_units(count):
    # type: (int) -> list[str]
    """`count` different Data-Codes."""
    return [flip(DATA_CODE, bits) for bits in range(count)]


def test_plan_index_keeps_registered_bounded_and_decodable_values():
    limit = binding.MAX_VALUE_BYTES
    kept = [block(seq([DATA_CODE, INSTANCE])), block(b"\x07" * limit, TRUSTMARK, {"region": 1})]
    dropped = [
        block(b"\x01", "com.example.unregistered"),
        block(b"\x07" * (limit + 1), TRUSTMARK),
        block(b"junk"),
        block(seq([META])),
    ]
    index = store.plan_index((*dropped[:2], kept[0], *dropped[2:], kept[1]))
    assert index.bindings == kept
    assert [u.iscc for u in index.units] == [DATA_CODE, INSTANCE]
    assert index.declarable == index.units


def test_plan_index_deduplicates_units_of_iscc_blocks_only():
    blocks = (block(seq([DATA_CODE]), TRUSTMARK), block(seq([META, DATA_CODE, INSTANCE])), block(seq([INSTANCE])))
    index = store.plan_index(blocks)
    assert index.bindings == list(blocks)
    assert [u.iscc for u in index.units] == [DATA_CODE, INSTANCE]
    assert [u.iscc for u in index.declarable] == [DATA_CODE, INSTANCE]


def test_plan_index_declares_only_units_of_whole_asset_blocks():
    index = store.plan_index((block(seq([CONTENT, DATA_CODE]), scope=TIME_SPAN), block(seq([DATA_CODE, INSTANCE]))))
    assert [u.iscc for u in index.units] == [CONTENT, DATA_CODE, INSTANCE]
    assert [u.iscc for u in index.declarable] == [DATA_CODE, INSTANCE]


def test_plan_index_of_scoped_blocks_alone_declares_nothing():
    index = store.plan_index((block(seq([DATA_CODE, INSTANCE]), scope={"region": {"type": "temporal"}}),))
    assert [u.iscc for u in index.units] == [DATA_CODE, INSTANCE]
    assert index.declarable == []
    assert store.plan_index(()) == store.Index(bindings=[], units=[], declarable=[])


def test_plan_index_limits_blocks():
    at_limit = [block(bytes([number]), TRUSTMARK) for number in range(store.MAX_BINDINGS)]
    assert len(store.plan_index(tuple(at_limit)).bindings) == store.MAX_BINDINGS
    ignored = [block(b"\x01", "com.example.unregistered"), block(b"\x07" * (binding.MAX_VALUE_BYTES + 1), TRUSTMARK)]
    assert len(store.plan_index((*at_limit, *ignored)).bindings) == store.MAX_BINDINGS
    with pytest.raises(manifest.InvalidManifestError) as error:
        store.plan_index((*at_limit, block(b"\xff", TRUSTMARK)))
    assert error.value.code == "manifest.tooManyBindings"
    assert error.value.message == (
        "This store indexes at most 64 soft-binding blocks, 8 of them io.iscc.v0 per manifest"
    )


def test_plan_index_limits_iscc_blocks():
    at_limit = [block(seq([unit, INSTANCE])) for unit in distinct_units(store.MAX_ISCC_BINDINGS)]
    assert len(store.plan_index(tuple(at_limit)).bindings) == store.MAX_ISCC_BINDINGS
    with pytest.raises(manifest.InvalidManifestError, match="tooManyBindings"):
        store.plan_index((*at_limit, block(b"junk")))  # an undecodable block counts too: it is decoded


def test_plan_index_limits_distinct_units():
    units = distinct_units(store.MAX_UNITS)
    index = store.plan_index((block(seq(units)), block(seq(units[:4]), scope=TIME_SPAN)))
    assert len(index.units) == len(index.declarable) == store.MAX_UNITS
    with pytest.raises(manifest.InvalidManifestError) as error:
        store.plan_index((block(seq(units)), block(seq([INSTANCE]), scope=TIME_SPAN)))
    assert error.value.code == "manifest.tooManyBindings"
    assert error.value.message == "This store indexes at most 32 distinct ISCC units per manifest"


def test_iscc_block_units():
    assert [u.iscc for u in store.iscc_block_units(block(seq([META, DATA_CODE])))] == [DATA_CODE]
    assert store.iscc_block_units(block(b"junk")) == []
    assert store.iscc_block_units(block(seq([META]))) == []
    assert store.iscc_block_units(block(b"")) == []


def test_unique_keeps_the_first_unit_of_each_type_and_body():
    data, instance = binding.decode_units(seq([DATA_CODE, INSTANCE]))
    assert store.unique([data, instance, data, instance]) == [data, instance]
    assert store.unique(iter([])) == []


# declaration plan and attempts


def test_declaration_plan_disabled_without_key_or_hub(settings, hub):
    units = binding.decode_units(seq([DATA_CODE, INSTANCE]))
    settings.ISCC_C2PA_STORE_SECKEY = ""
    assert store.declaration_plan(units, True) == {"status": Status.DISABLED}
    settings.ISCC_C2PA_STORE_SECKEY, settings.ISCC_C2PA_STORE_HUB_URL = SECKEY, ""
    assert store.declaration_plan(units, True) == {"status": Status.DISABLED}


def test_declaration_plan_skipped(hub):
    complete = binding.decode_units(seq([DATA_CODE, INSTANCE]))
    assert store.declaration_plan(complete, False) == {"status": Status.SKIPPED}
    assert store.declaration_plan(binding.decode_units(seq([CONTENT, DATA_CODE])), True) == {"status": Status.SKIPPED}
    assert store.declaration_plan([], True) == {"status": Status.SKIPPED}


def test_declaration_plan_pending(settings, hub):
    units = binding.decode_units(seq([CONTENT, DATA_CODE, INSTANCE]))
    settings.ISCC_C2PA_STORE_HUB_ID = 7
    assert store.declaration_plan(units, True) == {
        "status": Status.PENDING,
        "hub": "https://hub.example",
        "hub_id": 7,
        "units": [CONTENT, DATA_CODE, INSTANCE],
    }


@pytest.mark.django_db
def test_notes_keep_the_hub_id_of_the_declaration(settings, hub):
    settings.ISCC_C2PA_STORE_HUB_ID = 5
    hub.mode = "down"
    record = ingest(iscc_manifest())
    assert Declaration.objects.get(pk=record.pk).hub_id == 5
    settings.ISCC_C2PA_STORE_HUB_ID = 9  # the hub changes after the declaration was planned
    hub.mode = "ok"
    expire()
    assert store.retry_declarations() == 1
    declaration = Declaration.objects.get(pk=record.pk)
    assert declaration.status == Status.DECLARED
    assert nonce_hub(declaration.note) == 5
    store.delete(record.manifest_id)
    assert Declaration.objects.get(pk=record.pk).status == Status.WITHDRAWN
    assert hub.requests[-1].method == "DELETE"
    assert nonce_hub(json.loads(hub.requests[-1].content)) == 5
    assert [nonce_hub(json.loads(request.content)) for request in hub.requests] == [5, 5, 5]


@pytest.mark.django_db
def test_ingest_records_the_default_hub_id(hub):
    record = ingest(iscc_manifest())
    declaration = Declaration.objects.get(pk=record.pk)
    assert declaration.hub_id == 1
    assert nonce_hub(declaration.note) == 1
    assert Declaration.objects.get(pk=ingest(sign_manifest()).pk).hub_id == 0  # nothing to declare


@pytest.mark.django_db
def test_ingest_skips_declaration_on_request(hub):
    record = ingest(iscc_manifest(), declare_it=False)
    assert record.declaration.status == Status.SKIPPED
    assert hub.requests == []


@pytest.mark.django_db
def test_ingest_skips_declaration_without_data_and_instance_code(hub):
    record = ingest(sign_manifest([(TRUSTMARK, b64(b"\x01\x02"))]))
    assert record.declaration.status == Status.SKIPPED
    assert hub.requests == []


@pytest.mark.django_db
def test_ingest_declares_on_hub(hub):
    units = iscc_units(b"declared asset" * 50)
    record = ingest(iscc_manifest(units))
    declaration = Declaration.objects.get(pk=record.pk)
    assert declaration.status == Status.DECLARED
    assert declaration.attempts == 0  # reset on success: a fresh budget for withdrawing it later
    assert declaration.last_error == ""
    assert declaration.declared_at is not None
    assert declaration.hub == "https://hub.example"
    assert declaration.units == units
    assert declaration.iscc_id in hub.declared
    note = hub.declared[declaration.iscc_id]
    assert note == declaration.note
    assert note["gateway"] == store.manifest_url(record.manifest_id)
    assert note["iscc_code"] == declaration.iscc_code
    assert note["units"] == units[:1]
    assert hub.requests[0].url == "https://hub.example/declaration"


@pytest.mark.django_db
def test_ingest_refused_declaration_fails(hub):
    hub.mode = "refuse"
    declaration = Declaration.objects.get(pk=ingest(iscc_manifest()).pk)
    assert declaration.status == Status.FAILED
    assert declaration.attempts == 1
    assert declaration.last_error == "HTTP 422 invalid_iscc: refused"
    assert declaration.iscc_id == ""
    assert declaration.iscc_code.startswith("ISCC:")


@pytest.mark.django_db
@pytest.mark.parametrize(("mode", "error"), [("fail", "HTTP 503"), ("down", "ConnectError: hub down")])
def test_ingest_unreachable_hub_keeps_declaration_pending(hub, mode, error):
    hub.mode = mode
    record = ingest(iscc_manifest())
    assert record.declaration.status == Status.PENDING
    declaration = Declaration.objects.get(pk=record.pk)
    assert declaration.status == Status.PENDING
    assert declaration.attempts == 1
    assert declaration.last_error == error
    assert declaration.declared_at is None


@pytest.mark.django_db
def test_ingest_survives_malformed_hub_answer(settings, hub):
    settings.ISCC_C2PA_STORE_HUB_TRANSPORT = httpx2.MockTransport(created_without_json)
    record = ingest(iscc_manifest())
    declaration = Declaration.objects.get(pk=record.pk)
    assert declaration.status == Status.PENDING
    assert declaration.attempts == 1
    assert declaration.iscc_id == ""
    assert declaration.last_error == "HTTP 201 without an ISCC-ID"


@pytest.mark.django_db
def test_claim_takes_a_pending_declaration_once(hub):
    hub.mode = "down"
    record = ingest(iscc_manifest())
    expire()
    first, second = Declaration.objects.get(pk=record.pk), Declaration.objects.get(pk=record.pk)
    assert store.claim(first) is True
    assert store.claim(second) is False  # the other request counted an attempt meanwhile
    claimed = Declaration.objects.get(pk=record.pk)
    assert claimed.attempts == 2
    assert claimed.updated_at > datetime.now(UTC) - store.LEASE  # the lease is renewed
    assert store.claim(claimed) is True
    Declaration.objects.filter(pk=record.pk).update(status=Status.FAILED)
    assert store.claim(Declaration.objects.get(pk=record.pk)) is False


@pytest.mark.django_db
def test_attempt_skips_hub_when_claim_fails(hub):
    hub.mode = "down"
    record = ingest(iscc_manifest())
    stale_copy = Declaration.objects.get(pk=record.pk)
    assert store.claim(Declaration.objects.get(pk=record.pk))
    hub.mode = "ok"
    assert store.attempt(stale_copy) is stale_copy
    assert len(hub.requests) == 1
    declaration = Declaration.objects.get(pk=record.pk)
    assert declaration.status == Status.PENDING
    assert declaration.attempts == 2


@pytest.mark.django_db
def test_attempt_skips_hub_without_key(settings, hub):
    hub.mode = "down"
    record = ingest(iscc_manifest())
    settings.ISCC_C2PA_STORE_HUB_URL = ""
    hub.mode = "ok"
    declaration = store.attempt(Declaration.objects.get(pk=record.pk))
    assert declaration.status == Status.PENDING
    assert declaration.attempts == 1
    assert len(hub.requests) == 1


@pytest.mark.django_db
def test_attempt_records_declaration_of_manifest_deleted_meanwhile(settings, hub):
    hub.mode = "down"
    record = ingest(iscc_manifest())
    hub.mode = "ok"
    settings.ISCC_C2PA_STORE_HUB_TRANSPORT = httpx2.MockTransport(DeletingHub(hub, record.manifest_id))
    declaration = store.attempt(Declaration.objects.get(pk=record.pk))
    assert Manifest.objects.get(pk=record.pk).deleted_at is not None
    assert declaration.status == Status.DECLARED
    assert declaration.attempts == 0
    assert declaration.declared_at is not None
    assert declaration.iscc_id in hub.declared
    expire()
    assert store.retry_declarations() == 1  # the retry loop withdraws what the hub accepted too late
    assert Declaration.objects.get(pk=record.pk).status == Status.WITHDRAWN
    assert hub.declared == {}


@pytest.mark.django_db
@pytest.mark.parametrize("mode", ["refuse", "fail"])
def test_attempt_failure_leaves_withdrawn_declaration_alone(settings, hub, mode):
    hub.mode = "down"
    record = ingest(iscc_manifest())
    hub.mode = mode
    settings.ISCC_C2PA_STORE_HUB_TRANSPORT = httpx2.MockTransport(DeletingHub(hub, record.manifest_id))
    declaration = store.attempt(Declaration.objects.get(pk=record.pk))
    assert declaration.status == Status.WITHDRAWN
    assert declaration.last_error == "ConnectError: hub down"
    assert declaration.attempts == 2
    assert len(hub.requests) == 2


@pytest.mark.django_db
def test_requeue_makes_failed_declaration_pending(hub):
    hub.mode = "refuse"
    record = ingest(iscc_manifest())
    declaration = store.requeue(Declaration.objects.get(pk=record.pk))
    assert declaration.status == Status.PENDING
    assert declaration.attempts == 1
    hub.mode = "ok"
    declaration = store.attempt(declaration)
    assert declaration.status == Status.DECLARED
    assert declaration.last_error == ""
    assert store.requeue(declaration).status == Status.DECLARED


# fetch


@pytest.mark.django_db
def test_fetch_and_read_bytes():
    data = sign_manifest()
    record = store.fetch(ingest(data).manifest_id)
    assert store.read_bytes(record) == data


@pytest.mark.django_db
def test_fetch_unknown_manifest():
    with pytest.raises(store.NotFoundError):
        store.fetch(new_label())


@pytest.mark.django_db
@pytest.mark.parametrize("manifest_id", ["a" * 256, "urn:c2pa:\x00", "urn:c2pa:line\nbreak", "urn:c2pa:\u200b"])
def test_fetch_impossible_manifest_id(manifest_id):
    with pytest.raises(store.NotFoundError):
        store.fetch(manifest_id)


def test_is_manifest_id():
    assert store.is_manifest_id(new_label())
    assert store.is_manifest_id("contentauth:urn:uuid:b2b1f7fa-b119-4de1-9c0d-c97fbea3f2c3")
    assert store.is_manifest_id("a" * manifest.MAX_ID_LENGTH)
    assert store.is_manifest_id("urn:c2pa:café")
    assert not store.is_manifest_id("a" * (manifest.MAX_ID_LENGTH + 1))
    assert not store.is_manifest_id("urn:c2pa:tab\there")
    assert not store.is_manifest_id("urn:c2pa:\x7f")


@pytest.mark.django_db
def test_fetch_deleted_manifest():
    record = ingest(sign_manifest())
    store.delete(record.manifest_id)
    with pytest.raises(store.GoneError):
        store.fetch(record.manifest_id)


# delete


@pytest.mark.django_db
def test_delete_keeps_tombstone_without_bytes_or_index():
    data = iscc_manifest()
    record = ingest(data)
    blob = record.blob.name
    assert default_storage.exists(blob)
    deleted = store.delete(record.manifest_id)
    assert deleted.deleted_at is not None
    tombstone = Manifest.objects.get(pk=record.manifest_id)
    assert tombstone.deleted_at == deleted.deleted_at
    assert tombstone.sha256 == hashlib.sha256(data).hexdigest()
    assert tombstone.blob.name in ("", None)
    assert not default_storage.exists(blob)
    assert not Binding.objects.exists()
    assert not IsccUnit.objects.exists()
    assert Declaration.objects.get(pk=record.pk).status == Status.DISABLED


@pytest.mark.django_db
def test_delete_unknown_and_twice():
    with pytest.raises(store.NotFoundError):
        store.delete(new_label())
    record = ingest(sign_manifest())
    store.delete(record.manifest_id)
    with pytest.raises(store.GoneError):
        store.delete(record.manifest_id)


@pytest.mark.django_db
def test_delete_withdraws_declaration(hub):
    record = ingest(iscc_manifest())
    iscc_id = record.declaration.iscc_id
    assert iscc_id in hub.declared
    store.delete(record.manifest_id)
    declaration = Declaration.objects.get(pk=record.pk)
    assert declaration.status == Status.WITHDRAWN
    assert declaration.attempts == 1
    assert declaration.last_error == ""
    assert hub.declared == {}
    assert hub.requests[-1].method == "DELETE"
    assert hub.requests[-1].url == f"https://hub.example/declaration/{iscc_id}"


@pytest.mark.django_db
def test_delete_pending_declaration_is_withdrawn_without_hub(hub):
    hub.mode = "down"
    record = ingest(iscc_manifest())
    store.delete(record.manifest_id)
    assert Declaration.objects.get(pk=record.pk).status == Status.WITHDRAWN
    assert len(hub.requests) == 1


@pytest.mark.django_db
def test_delete_failed_declaration_is_withdrawn_without_hub(hub):
    hub.mode = "refuse"
    record = ingest(iscc_manifest())
    assert Declaration.objects.get(pk=record.pk).status == Status.FAILED
    store.delete(record.manifest_id)
    declaration = Declaration.objects.get(pk=record.pk)
    assert declaration.status == Status.WITHDRAWN
    assert declaration.attempts == 1
    assert len(hub.requests) == 1


@pytest.mark.django_db
@pytest.mark.parametrize(("mode", "error"), [("fail", "HTTP 503"), ("refuse", "HTTP 422 invalid_iscc: refused")])
def test_delete_failed_withdrawal_stays_declared(hub, mode, error):
    record = ingest(iscc_manifest())
    hub.mode = mode
    store.delete(record.manifest_id)
    declaration = Declaration.objects.get(pk=record.pk)
    assert declaration.status == Status.DECLARED
    assert declaration.attempts == 1
    assert declaration.last_error == error
    assert declaration.iscc_id in hub.declared


@pytest.mark.django_db
def test_delete_without_key_keeps_declaration(settings, hub):
    record = ingest(iscc_manifest())
    settings.ISCC_C2PA_STORE_SECKEY = ""
    store.delete(record.manifest_id)
    declaration = Declaration.objects.get(pk=record.pk)
    assert declaration.status == Status.DECLARED
    assert declaration.attempts == 1
    assert declaration.last_error == "Withdrawal needs the declaration key"
    assert len(hub.requests) == 1


@pytest.mark.django_db
def test_withdraw_only_updates_declared(hub):
    record = ingest(iscc_manifest())
    declaration = Declaration.objects.get(pk=record.pk)
    Declaration.objects.filter(pk=record.pk).update(status=Status.WITHDRAWN)
    withdrawn = store.withdraw(declaration)
    assert withdrawn.status == Status.WITHDRAWN
    assert withdrawn.attempts == 0
    assert withdrawn.last_error == ""


@pytest.mark.django_db
def test_delete_withdraws_undeclared_declaration_in_the_same_transaction(hub):
    hub.mode = "down"
    data = iscc_manifest()
    record = ingest(data)
    with connection.execute_wrapper(failing_declaration_update), pytest.raises(OperationalError):
        store.delete(record.manifest_id)
    live = store.fetch(record.manifest_id)
    assert live.bindings.exists()
    assert live.units.exists()
    assert store.read_bytes(live) == data
    assert Declaration.objects.get(pk=record.pk).status == Status.PENDING


@pytest.mark.django_db
def test_read_bytes_of_manifest_deleted_after_fetch():
    record = store.fetch(ingest(sign_manifest()).manifest_id)
    store.delete(record.manifest_id)
    with pytest.raises(store.GoneError):
        store.read_bytes(record)


@pytest.mark.django_db
def test_read_bytes_of_live_manifest_without_file(caplog):
    record = store.fetch(ingest(sign_manifest()).manifest_id)
    default_storage.delete(record.blob.name)
    with caplog.at_level(logging.ERROR, logger=store.log.name), pytest.raises(store.NotFoundError):
        store.read_bytes(record)
    assert [r.getMessage() for r in caplog.records] == [
        f"The file of manifest {record.manifest_id} is missing: {record.blob.name}"
    ]
    assert caplog.records[0].levelno == logging.ERROR


# release


@pytest.mark.django_db
def test_release_frees_the_id_and_bytes_of_a_deleted_manifest():
    label = new_label()
    squatter = signed(label=label, title="squatter.jpg")
    ingest(squatter)
    assert store.release(label) is False  # live
    store.delete(label)
    expire()
    assert store.release(label) is True
    assert not Manifest.objects.exists()
    assert not Declaration.objects.exists()
    assert store.release(label) is False  # nothing left to release
    assert ingest(signed(label=label, title="genuine.jpg")).title == "genuine.jpg"
    store.delete(label)
    expire()
    assert store.release(label) is True
    assert ingest(squatter).manifest_id == label  # the same bytes can come back too


@pytest.mark.django_db
def test_release_unknown_manifest():
    assert store.release(new_label()) is False


@pytest.mark.django_db
def test_release_waits_for_the_withdrawal(hub):
    record = ingest(iscc_manifest())
    hub.mode = "fail"
    store.delete(record.manifest_id)
    assert store.release(record.manifest_id) is False
    assert Manifest.objects.filter(pk=record.pk).exists()
    hub.mode = "ok"
    expire()
    assert store.retry_declarations() == 1
    expire()
    assert store.release(record.manifest_id) is True
    assert not Manifest.objects.exists()


@pytest.mark.django_db
def test_release_of_a_declaration_that_never_reached_the_hub(hub):
    hub.mode = "down"
    record = ingest(iscc_manifest())
    store.delete(record.manifest_id)
    assert Declaration.objects.get(pk=record.pk).status == Status.WITHDRAWN
    expire()
    assert store.release(record.manifest_id) is True
    assert not Declaration.objects.exists()


@pytest.mark.django_db
def test_release_waits_while_a_declaration_request_may_be_in_flight(hub):
    """A pending declaration withdrawn by delete() may still be on its way to the hub for up to LEASE."""
    hub.mode = "down"
    record = ingest(iscc_manifest())
    store.delete(record.manifest_id)
    assert store.release(record.manifest_id) is False
    age(store.LEASE + timedelta(seconds=1))
    assert store.release(record.manifest_id) is True


@pytest.mark.django_db
def test_release_removes_a_file_left_behind_by_a_failed_delete():
    data = iscc_manifest()
    record = ingest(data)
    store.delete(record.manifest_id)
    leftover = default_storage.save(f"manifests/{record.sha256}.c2pa", ContentFile(data))
    expire()
    assert store.release(record.manifest_id) is True
    assert not default_storage.exists(leftover)
    assert ingest(data).blob.name == leftover  # the same bytes get their plain name again


# retries


@pytest.mark.django_db
def test_retry_declares_pending_and_withdraws_orphaned(hub):
    hub.mode = "down"
    pending = ingest(iscc_manifest())
    hub.mode = "ok"
    orphaned = ingest(iscc_manifest())
    hub.mode = "fail"
    store.delete(orphaned.manifest_id)
    hub.mode = "ok"
    expire()
    assert store.retry_declarations() == 2
    declared = Declaration.objects.get(pk=pending.pk)
    assert declared.status == Status.DECLARED
    assert declared.attempts == 0
    assert declared.last_error == ""
    withdrawn = Declaration.objects.get(pk=orphaned.pk)
    assert withdrawn.status == Status.WITHDRAWN
    assert withdrawn.attempts == 2
    assert list(hub.declared) == [declared.iscc_id]
    expire()
    assert store.retry_declarations() == 0


@pytest.mark.django_db
def test_retry_leaves_recent_declarations_alone(hub):
    hub.mode = "down"
    pending = ingest(iscc_manifest())
    hub.mode = "ok"
    orphaned = ingest(iscc_manifest())
    hub.mode = "fail"
    store.delete(orphaned.manifest_id)
    hub.mode = "ok"
    assert store.retry_declarations() == 0
    assert len(hub.requests) == 3
    assert Declaration.objects.get(pk=pending.pk).status == Status.PENDING
    assert Declaration.objects.get(pk=orphaned.pk).status == Status.DECLARED


@pytest.mark.django_db
def test_retry_gives_up_seven_days_after_the_upload(hub):
    hub.mode = "down"
    old = ingest(iscc_manifest())
    recent = ingest(iscc_manifest())
    Manifest.objects.filter(pk=old.pk).update(uploaded_at=datetime.now(UTC) - store.GIVE_UP - timedelta(minutes=1))
    expire()
    assert store.retry_declarations() == 1
    given_up = Declaration.objects.get(pk=old.pk)
    assert given_up.status == Status.FAILED
    assert given_up.last_error == "Gave up after 7 days"
    assert given_up.attempts == 1
    assert given_up.updated_at > datetime.now(UTC) - store.LEASE
    assert Declaration.objects.get(pk=recent.pk).attempts == 2
    assert len(hub.requests) == 3


@pytest.mark.django_db
def test_retry_never_gives_up_on_withdrawals(hub):
    record = ingest(iscc_manifest())
    hub.mode = "fail"
    store.delete(record.manifest_id)
    Manifest.objects.filter(pk=record.pk).update(uploaded_at=datetime.now(UTC) - store.GIVE_UP * 2)
    Declaration.objects.filter(pk=record.pk).update(attempts=100)
    hub.mode = "ok"
    expire()
    assert store.retry_declarations() == 1
    assert Declaration.objects.get(pk=record.pk).status == Status.WITHDRAWN
    assert hub.declared == {}


@pytest.mark.django_db
def test_retry_backs_off_per_attempt(hub):
    hub.mode = "down"
    record = ingest(iscc_manifest())  # one attempt: due two minutes after it
    age(timedelta(seconds=110))
    assert store.retry_declarations() == 0
    age(timedelta(seconds=121))
    assert store.retry_declarations() == 1  # two attempts: due four minutes after the last
    age(timedelta(minutes=3))
    assert store.retry_declarations() == 0
    age(timedelta(minutes=4, seconds=1))
    assert store.retry_declarations() == 1
    Declaration.objects.filter(pk=record.pk).update(attempts=30)
    age(store.MAX_BACKOFF - timedelta(seconds=10))
    assert store.retry_declarations() == 0
    expire()
    assert store.retry_declarations() == 1
    declaration = Declaration.objects.get(pk=record.pk)
    assert declaration.status == Status.PENDING
    assert declaration.attempts == 31
    assert len(hub.requests) == 4


@pytest.mark.parametrize(
    ("attempts", "seconds"), [(0, 60), (1, 120), (2, 240), (5, 1920), (6, 3600), (12, 3600), (10**6, 3600)]
)
def test_is_due_once_the_backoff_has_passed(attempts, seconds):
    now = datetime(2026, 1, 1, tzinfo=UTC)
    declaration = Declaration(attempts=attempts, updated_at=now - timedelta(seconds=seconds))
    assert store.is_due(declaration, now)
    declaration.updated_at += timedelta(seconds=1)
    assert not store.is_due(declaration, now)


@pytest.mark.django_db
def test_retry_skips_pending_of_deleted_manifest(hub):
    hub.mode = "down"
    record = ingest(iscc_manifest())
    store.delete(record.manifest_id)
    Declaration.objects.filter(pk=record.pk).update(status=Status.PENDING)
    hub.mode = "ok"
    expire()
    assert store.retry_declarations() == 0
    assert Declaration.objects.get(pk=record.pk).status == Status.PENDING
    assert len(hub.requests) == 1


@pytest.mark.django_db
def test_retry_without_key_leaves_pending(settings, hub):
    hub.mode = "down"
    record = ingest(iscc_manifest())
    settings.ISCC_C2PA_STORE_SECKEY = ""
    expire()
    assert store.retry_declarations() == 1
    declaration = Declaration.objects.get(pk=record.pk)
    assert declaration.status == Status.PENDING
    assert declaration.attempts == 1
    assert len(hub.requests) == 1


@pytest.mark.django_db
def test_retry_withdraws_after_late_declaration(hub):
    hub.mode = "down"
    record = ingest(iscc_manifest())
    Declaration.objects.filter(pk=record.pk).update(attempts=40)
    hub.mode = "ok"
    expire()
    assert store.retry_declarations() == 1
    declared = Declaration.objects.get(pk=record.pk)
    assert declared.status == Status.DECLARED
    assert declared.attempts == 0
    hub.mode = "fail"
    store.delete(record.manifest_id)
    hub.mode = "ok"
    age(store.LEASE * 2 + timedelta(seconds=1))  # the backoff after one failed withdrawal
    assert store.retry_declarations() == 1
    assert Declaration.objects.get(pk=record.pk).status == Status.WITHDRAWN


# queries


def test_algorithms_registry():
    registry = store.algorithms()
    assert registry["io.iscc.v0"] == "fingerprint"
    assert registry[TRUSTMARK] == "watermark"
    assert registry["com.adobe.icn.dense"] == "fingerprint"
    assert set(registry.values()) == {"fingerprint", "watermark"}


@pytest.mark.django_db
def test_query_exact_match_for_registered_algorithm():
    value = b"\x00\xffarbitrary watermark payload"
    record = ingest(sign_manifest([(TRUSTMARK, b64(value))]))
    ingest(sign_manifest([(TRUSTMARK, b64(b"other payload"))]))
    ingest(sign_manifest([("com.adobe.trustmark.C", b64(value))]))
    found = store.query(TRUSTMARK, value, 10)
    assert [(m.manifest_id, m.score, m.iscc_id, m.trusted) for m in found] == [(record.manifest_id, 100, "", False)]
    assert found[0].uploaded_at == Manifest.objects.get(pk=record.pk).uploaded_at
    assert store.query(TRUSTMARK, b"unknown payload", 10) == []


@pytest.mark.django_db
def test_query_unknown_algorithm():
    with pytest.raises(ValueError, match="Unknown soft binding algorithm"):
        store.query("com.example.unknown", b"x", 10)


@pytest.mark.django_db
def test_query_invalid_iscc_value():
    with pytest.raises(ValueError):
        store.query("io.iscc.v0", b"junk", 10)
    with pytest.raises(ValueError):
        store.query("io.iscc.v0", seq([META]), 10)


@pytest.mark.django_db
def test_query_refuses_more_than_max_units():
    units = distinct_units(store.MAX_UNITS + 1)
    with pytest.raises(ValueError, match="more than 32 searchable ISCC units"):
        store.query("io.iscc.v0", seq(units), 10)
    assert store.query("io.iscc.v0", seq(units[:-1]), 10) == []


@pytest.mark.django_db
def test_query_iscc_exact_value():
    record = ingest(sign_manifest([("io.iscc.v0", VECTOR_1)]))
    found = store.query("io.iscc.v0", base64.b64decode(VECTOR_1), 10)
    assert [(m.manifest_id, m.score) for m in found] == [(record.manifest_id, 100)]


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("unit", "bits", "score"),
    [(DATA_CODE, 0, 99), (DATA_CODE, 26, 90), (DATA_CODE, 65, 75), (CONTENT, 10, 96), (CONTENT, 64, 75)],
)
def test_query_iscc_similarity_at_or_above_threshold(unit, bits, score):
    record = ingest(sign_manifest([("io.iscc.v0", VECTOR_1)]))
    found = store.query("io.iscc.v0", seq([flip(unit, bits)]), 10)
    assert [(m.manifest_id, m.score) for m in found] == [(record.manifest_id, score)]


@pytest.mark.django_db
@pytest.mark.parametrize("bits", [66, 79, 128, 256])
def test_query_iscc_similarity_below_threshold(bits):
    ingest(sign_manifest([("io.iscc.v0", VECTOR_1)]))
    assert store.query("io.iscc.v0", seq([flip(DATA_CODE, bits)]), 10) == []


@pytest.mark.django_db
def test_query_threshold_setting(settings):
    record = ingest(sign_manifest([("io.iscc.v0", VECTOR_1)]))
    settings.ISCC_C2PA_STORE_MATCH_THRESHOLD = 50
    found = store.query("io.iscc.v0", seq([flip(DATA_CODE, 128)]), 10)
    assert [(m.manifest_id, m.score) for m in found] == [(record.manifest_id, 50)]


@pytest.mark.django_db
def test_query_instance_code_equality_scores_100():
    record = ingest(sign_manifest([("io.iscc.v0", VECTOR_1)]))
    found = store.query("io.iscc.v0", seq([INSTANCE]), 10)
    assert [(m.manifest_id, m.score) for m in found] == [(record.manifest_id, 100)]
    assert store.query("io.iscc.v0", seq([flip(INSTANCE, 1)]), 10) == []


@pytest.mark.django_db
def test_query_best_unit_wins():
    record = ingest(sign_manifest([("io.iscc.v0", VECTOR_1)]))
    found = store.query("io.iscc.v0", seq([flip(CONTENT, 64), flip(DATA_CODE, 10), flip(INSTANCE, 1)]), 10)
    assert [(m.manifest_id, m.score) for m in found] == [(record.manifest_id, 96)]


@pytest.mark.django_db
def test_query_meta_code_never_counts():
    ingest(sign_manifest([("io.iscc.v0", VECTOR_1)]))
    assert store.query("io.iscc.v0", seq([META, flip(DATA_CODE, 128)]), 10) == []


@pytest.mark.django_db
def test_query_reads_the_units_of_each_type_once():
    near = ingest(sign_manifest([("io.iscc.v0", encode([DATA_CODE]))]))
    far = ingest(sign_manifest([("io.iscc.v0", encode([flip(DATA_CODE, 200)]))]))
    value = seq([flip(DATA_CODE, 10), flip(DATA_CODE, 210), flip(INSTANCE, 1)])  # two Data-Codes, 10 bits off each
    with CaptureQueriesContext(connection) as queries:
        found = store.query("io.iscc.v0", value, 10)
    assert {(m.manifest_id, m.score) for m in found} == {(near.manifest_id, 96), (far.manifest_id, 96)}
    unit_reads = [q["sql"] for q in queries.captured_queries if 'FROM "iscc_c2pa_store_isccunit"' in q["sql"]]
    assert len(unit_reads) == 2  # Data-Codes and Instance-Codes


def test_iscc_scores_without_query_units():
    assert store.iscc_scores([]) == {}


@pytest.mark.django_db
def test_query_ranks_by_score_then_upload_time():
    similar = ingest(sign_manifest([("io.iscc.v0", encode([flip(DATA_CODE, 20)]))]))
    later = ingest(sign_manifest([("io.iscc.v0", encode([DATA_CODE]))]))
    earlier = ingest(sign_manifest([("io.iscc.v0", encode([DATA_CODE]))]))
    backdate(similar, 30)
    backdate(later, 10)
    backdate(earlier, 20)
    query = seq([DATA_CODE, CONTENT])  # not equal to any stored value: scores come from similarity alone
    found = store.query("io.iscc.v0", query, 10)
    assert [(m.manifest_id, m.score) for m in found] == [
        (earlier.manifest_id, 99),
        (later.manifest_id, 99),
        (similar.manifest_id, 92),
    ]
    assert [m.manifest_id for m in store.query("io.iscc.v0", query, 2)] == [
        earlier.manifest_id,
        later.manifest_id,
    ]


@pytest.mark.django_db
def test_query_ranks_trusted_signers_first_at_equal_score(settings, trust_anchors):
    value = b"public watermark"
    untrusted = ingest(sign_manifest([(TRUSTMARK, b64(value))]))
    settings.ISCC_C2PA_STORE_TRUST_ANCHORS = trust_anchors
    trusted = ingest(sign_manifest([(TRUSTMARK, b64(value))]))
    backdate(untrusted, 10)
    assert [(m.manifest_id, m.trusted) for m in store.query(TRUSTMARK, value, 10)] == [
        (trusted.manifest_id, True),
        (untrusted.manifest_id, False),
    ]
    assert [m.manifest_id for m in store.query(TRUSTMARK, value, 1)] == [trusted.manifest_id]


@pytest.mark.django_db
def test_ranked_orders_by_score_trust_upload_time_and_id():
    Manifest.objects.bulk_create(
        [bare("urn:c2pa:a"), bare("urn:c2pa:b"), bare("urn:c2pa:c", True), bare("urn:c2pa:d", True), bare("urn:c2pa:e")]
    )
    Manifest.objects.update(uploaded_at=datetime(2026, 1, 1, tzinfo=UTC))
    Manifest.objects.filter(pk="urn:c2pa:e").update(uploaded_at=datetime(2025, 1, 1, tzinfo=UTC))
    scores = {"urn:c2pa:b": 90, "urn:c2pa:c": 80, "urn:c2pa:a": 90, "urn:c2pa:e": 90, "urn:c2pa:d": 90}
    found = store.ranked(scores, 10)
    assert [(m.manifest_id, m.score, m.trusted) for m in found] == [
        ("urn:c2pa:d", 90, True),  # trusted first at equal score
        ("urn:c2pa:e", 90, False),  # then the earliest upload
        ("urn:c2pa:a", 90, False),  # then the ID
        ("urn:c2pa:b", 90, False),
        ("urn:c2pa:c", 80, True),  # trust never beats a higher score
    ]
    assert [m.manifest_id for m in store.ranked(scores, 2)] == ["urn:c2pa:d", "urn:c2pa:e"]
    assert store.ranked({}, 10) == []


@pytest.mark.django_db
def test_ranked_considers_only_the_best_candidates():
    ids = [f"urn:c2pa:{uuid.UUID(int=number)}" for number in range(store.MAX_CANDIDATES + 1)]
    Manifest.objects.bulk_create(bare(manifest_id, trusted=manifest_id == ids[0]) for manifest_id in ids)
    scores = {ids[0]: 80} | dict.fromkeys(ids[1:], 90)
    found = store.ranked(scores, len(ids))
    assert len(found) == store.MAX_CANDIDATES
    assert ids[0] not in {m.manifest_id for m in found}
    assert {m.score for m in found} == {90}


@pytest.mark.django_db
def test_query_caps_results_at_100():
    value = b"shared watermark"
    records = Manifest.objects.bulk_create(
        bare(f"urn:c2pa:{uuid.UUID(int=number)}") for number in range(store.MAX_RESULTS + 1)
    )
    Binding.objects.bulk_create(Binding(manifest=record, alg=TRUSTMARK, value=value) for record in records)
    assert len(store.query(TRUSTMARK, value, 1000)) == store.MAX_RESULTS
    assert len(store.query(TRUSTMARK, value, 5)) == 5


@pytest.mark.django_db
def test_query_excludes_deleted_manifests():
    value = b"deleted watermark"
    gone = ingest(sign_manifest([(TRUSTMARK, b64(value)), ("io.iscc.v0", VECTOR_1)]))
    store.delete(gone.manifest_id)
    assert store.query(TRUSTMARK, value, 10) == []
    assert store.query("io.iscc.v0", seq([INSTANCE]), 10) == []
    assert store.query("io.iscc.v0", base64.b64decode(VECTOR_1), 10) == []


@pytest.mark.django_db
def test_query_attaches_iscc_id(hub):
    units = iscc_units(b"findable asset" * 50)
    record = ingest(iscc_manifest(units))
    found = store.query("io.iscc.v0", seq(units[1:]), 10)
    assert record.declaration.iscc_id
    assert [(m.manifest_id, m.iscc_id) for m in found] == [(record.manifest_id, record.declaration.iscc_id)]


# status and figures


@pytest.mark.django_db
def test_supported_algorithms_by_type():
    assert store.supported_algorithms() == {"fingerprint": ["io.iscc.v0"], "watermark": []}
    ingest(
        sign_manifest(
            [(TRUSTMARK, b64(b"w")), ("com.adobe.icn.dense", b64(b"f")), ("com.example.unregistered", b64(b"u"))]
        )
    )
    assert store.supported_algorithms() == {
        "fingerprint": ["com.adobe.icn.dense", "io.iscc.v0"],
        "watermark": [TRUSTMARK],
    }


@pytest.mark.django_db
def test_service_status_degrades_with_stale_pending_declaration(hub):
    assert store.service_status() == "ok"
    hub.mode = "down"
    record = ingest(iscc_manifest())
    assert store.service_status() == "ok"
    backdate(record, 16)
    assert store.service_status() == "degraded"
    hub.mode = "ok"
    expire()
    store.retry_declarations()
    assert store.service_status() == "ok"


@pytest.mark.django_db
def test_statistics(settings, trust_anchors):
    assert store.statistics() == {"manifests": 0, "trusted": 0, "deleted": 0, "bindings": {}, "declarations": {}}
    ingest(sign_manifest([(TRUSTMARK, b64(b"one"))]))
    ingest(sign_manifest([(TRUSTMARK, b64(b"two")), ("io.iscc.v0", VECTOR_1)]))
    store.delete(ingest(sign_manifest([("io.iscc.v0", VECTOR_1)])).manifest_id)
    settings.ISCC_C2PA_STORE_TRUST_ANCHORS = trust_anchors
    ingest(sign_manifest())
    assert store.statistics() == {
        "manifests": 3,
        "trusted": 1,
        "deleted": 1,
        "bindings": {TRUSTMARK: 2, "io.iscc.v0": 1},
        "declarations": {"disabled": 3},
    }


@pytest.mark.django_db
def test_statistics_count_declarations_of_live_manifests(hub):
    declared = ingest(iscc_manifest())
    ingest(iscc_manifest(), declare_it=False)
    store.delete(ingest(iscc_manifest()).manifest_id)
    assert store.statistics()["declarations"] == {"declared": 1, "skipped": 1}
    store.delete(declared.manifest_id)
    assert store.statistics()["declarations"] == {"skipped": 1}


@pytest.mark.django_db
def test_cached_statistics():
    assert store.cached_statistics()["manifests"] == 0
    ingest(sign_manifest())
    assert store.cached_statistics()["manifests"] == 0
    cache.clear()
    assert store.cached_statistics() == store.statistics()
    assert store.cached_statistics()["manifests"] == 1


def test_declares(settings):
    assert store.declares() is False
    assert store.keypair() is None
    settings.ISCC_C2PA_STORE_SECKEY = SECKEY
    assert store.declares() is False
    settings.ISCC_C2PA_STORE_HUB_URL = "https://hub.example"
    assert store.declares() is True
    assert store.keypair() is not None
    settings.ISCC_C2PA_STORE_SECKEY = ""
    assert store.declares() is False


def test_uploader_key(settings):
    key = store.uploader_key("203.0.113.7")
    expected = hmac.new(settings.SECRET_KEY.encode(), b"203.0.113.7", hashlib.sha256).hexdigest()[:32]
    assert key == expected
    assert len(key) == 32
    assert "203.0.113.7" not in key
    assert store.uploader_key("203.0.113.8") != key
    settings.SECRET_KEY = "another-test-only-secret"  # noqa: S105 (test value)
    assert store.uploader_key("203.0.113.7") != key


def test_manifest_url():
    label = "urn:c2pa:0b2c3d4e-0000-4000-8000-000000000000"
    assert store.manifest_url(label) == f"https://store.example/v1/manifests/{label.replace(':', '%3A')}"
    assert store.manifest_url("a/b c") == "https://store.example/v1/manifests/a%2Fb%20c"


def test_timestamp_is_aware():
    assert store.timestamp().tzinfo is not None
