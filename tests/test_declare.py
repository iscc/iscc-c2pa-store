"""Tests of ISCC declarations: unit choice, signed IsccNotes in the shape iscc-hub validates, and the hub client."""

import functools
import json
import re
from datetime import UTC, datetime, timedelta, timezone

import httpx2
import iscc_core as ic
import iscc_crypto as icr
import pytest
from conftest import CONTENT, DATA_CODE, HUB_URL, INSTANCE, META, SECKEY

from iscc_c2pa_store.binding import searchable
from iscc_c2pa_store.declare import (
    BLAKE3_MULTIHASH,
    DELETE_SCHEMA,
    NOTE_SCHEMA,
    RETRYABLE,
    Outcome,
    build_delete,
    build_note,
    declarable_units,
    gateway_url,
    hub_error,
    is_final,
    issued_id,
    submit,
    timestamp,
    withdraw,
)

CONTROLLER = "did:web:store.example"
KEYPAIR = icr.key_from_secret(SECKEY, controller=CONTROLLER)
NOW = datetime(2026, 10, 9, 12, 34, 56, 789123, tzinfo=UTC)
GATEWAY = "https://store.example/v1/manifests/urn%3Ac2pa%3A1234"
ISCC_ID = "ISCC:" + ic.encode_component(
    ic.MT.ID, ic.ST_ID_REALM.REALM_0, ic.VS.V1, 64, (1 << 52 | 1).to_bytes(8, "big")
)
SEMANTIC_TEXT = "ISCC:" + ic.encode_component(ic.MT.SEMANTIC, ic.ST_CC.TEXT, ic.VS.V0, 256, bytes(range(32)))
SEMANTIC_IMAGE = "ISCC:" + ic.encode_component(ic.MT.SEMANTIC, ic.ST_CC.IMAGE, ic.VS.V0, 256, bytes(range(32)))
CONTENT_IMAGE = "ISCC:" + ic.encode_component(ic.MT.CONTENT, ic.ST_CC.IMAGE, ic.VS.V0, 256, bytes(32))
OTHER_DATA = "ISCC:" + ic.encode_component(ic.MT.DATA, ic.ST.NONE, ic.VS.V0, 256, bytes(32))
# Field rules of iscc-hub (iscc_hub/validators.py) the notes must satisfy
NOTE_FIELDS = {"$schema", "iscc_code", "datahash", "nonce", "timestamp", "signature", "units", "metahash", "gateway"}
DELETE_FIELDS = {"$schema", "iscc_id", "nonce", "timestamp", "signature"}
SIGNATURE_FIELDS = {"version", "proof", "pubkey", "controller", "keyid"}
HUB_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
HUB_MAX_STRING = 2048
HUB_MAX_UNITS = 4


def units(*isccs):
    # type: (str) -> list
    """Searchable units of ISCC strings, as the store keeps them."""
    return [searchable(iscc) for iscc in isccs]


def reply(seen, status, kwargs, request):
    # type: (list, int, dict, httpx2.Request) -> httpx2.Response
    """Record a request and answer it with a fixed status and response arguments (json, text or content)."""
    seen.append(request)
    return httpx2.Response(status, **kwargs)


def unreachable(request):
    # type: (httpx2.Request) -> httpx2.Response
    """A hub that cannot be connected to."""
    raise httpx2.ConnectError("hub down", request=request)


def timing_out(request):
    # type: (httpx2.Request) -> httpx2.Response
    """A hub that does not answer in time."""
    raise httpx2.ReadTimeout("timed out", request=request)


def hub_client(status, seen=None, **kwargs):
    # type: (int, list | None, object) -> httpx2.Client
    """An httpx2 client whose hub answers every request with `status` and records it in `seen`."""
    handler = functools.partial(reply, [] if seen is None else seen, status, kwargs)
    return httpx2.Client(transport=httpx2.MockTransport(handler))


def failing_client(handler):
    # type: (object) -> httpx2.Client
    """An httpx2 client whose transport raises a network error."""
    return httpx2.Client(transport=httpx2.MockTransport(handler))


def nonce_hub_id(nonce):
    # type: (str) -> int
    """The hub ID in the first 12 bits of a nonce, read the way iscc-hub reads it."""
    raw = bytes.fromhex(nonce)
    return (raw[0] << 4) | (raw[1] >> 4)


def instance_from_datahash(datahash):
    # type: (str) -> str
    """The 256-bit Instance-Code iscc-hub derives from a datahash."""
    return "ISCC:" + ic.encode_component(ic.MT.INSTANCE, ic.ST.NONE, ic.VS.V0, 256, bytes.fromhex(datahash[4:]))


def test_declarable_units_in_maintype_order():
    given = units(INSTANCE, DATA_CODE, CONTENT, SEMANTIC_TEXT)
    assert declarable_units(given) == [SEMANTIC_TEXT, CONTENT, DATA_CODE, INSTANCE]


def test_declarable_units_data_and_instance_only():
    assert declarable_units(units(DATA_CODE, INSTANCE)) == [DATA_CODE, INSTANCE]


def test_declarable_units_content_data_instance():
    assert declarable_units(units(CONTENT, DATA_CODE, INSTANCE)) == [CONTENT, DATA_CODE, INSTANCE]


def test_declarable_units_keep_semantic_without_content():
    assert declarable_units(units(SEMANTIC_IMAGE, DATA_CODE, INSTANCE)) == [SEMANTIC_IMAGE, DATA_CODE, INSTANCE]


def test_declarable_units_drop_semantic_of_other_subtype():
    assert declarable_units(units(SEMANTIC_IMAGE, CONTENT, DATA_CODE, INSTANCE)) == [CONTENT, DATA_CODE, INSTANCE]


def test_declarable_units_keep_semantic_of_same_subtype():
    given = units(SEMANTIC_IMAGE, CONTENT_IMAGE, DATA_CODE, INSTANCE)
    assert declarable_units(given) == [SEMANTIC_IMAGE, CONTENT_IMAGE, DATA_CODE, INSTANCE]


def test_declarable_units_take_the_first_unit_of_each_maintype():
    given = units(CONTENT_IMAGE, CONTENT, OTHER_DATA, DATA_CODE, INSTANCE)
    assert declarable_units(given) == [CONTENT_IMAGE, OTHER_DATA, INSTANCE]


def test_declarable_units_need_data_code():
    assert declarable_units(units(CONTENT, INSTANCE)) == []


def test_declarable_units_need_instance_code():
    assert declarable_units(units(SEMANTIC_TEXT, CONTENT, DATA_CODE)) == []


def test_declarable_units_of_nothing():
    assert declarable_units([]) == []


def test_declarable_units_build_an_iscc_code():
    for given in (
        [DATA_CODE, INSTANCE],
        [SEMANTIC_IMAGE, CONTENT, DATA_CODE, INSTANCE],
        [SEMANTIC_TEXT, DATA_CODE, INSTANCE],
    ):
        assert ic.gen_iscc_code(declarable_units(units(*given)))["iscc"].startswith("ISCC:K")


@pytest.mark.parametrize(
    ("base", "manifest_id", "expected"),
    [
        ("https://store.example", "urn:c2pa:1234", "https://store.example/v1/manifests/urn%3Ac2pa%3A1234"),
        ("https://store.example/", "urn:c2pa:1234", "https://store.example/v1/manifests/urn%3Ac2pa%3A1234"),
        ("https://store.example/sub", "a b", "https://store.example/sub/v1/manifests/a%20b"),
        ("https://store.example", "a/b/../c", "https://store.example/v1/manifests/a%2Fb%2F..%2Fc"),
        ("https://store.example", "x?y#z", "https://store.example/v1/manifests/x%3Fy%23z"),
        ("https://store.example", "{iscc_id}", "https://store.example/v1/manifests/%7Biscc_id%7D"),
        ("https://store.example", "ü+%", "https://store.example/v1/manifests/%C3%BC%2B%25"),
        ("https://store.example", "a-b_c.d~e", "https://store.example/v1/manifests/a-b_c.d~e"),
    ],
)
def test_gateway_url(base, manifest_id, expected):
    assert gateway_url(base, manifest_id) == expected


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (NOW, "2026-10-09T12:34:56.789Z"),
        (datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC), "2026-01-02T03:04:05.000Z"),
        (datetime(2026, 1, 2, 3, 4, 5, 999999, tzinfo=UTC), "2026-01-02T03:04:05.999Z"),
        (datetime(2026, 1, 2, 3, 4, 5, 1000, tzinfo=timezone(timedelta(hours=2))), "2026-01-02T01:04:05.001Z"),
        (datetime(2026, 1, 1, 23, 0, tzinfo=timezone(timedelta(hours=-5))), "2026-01-02T04:00:00.000Z"),
    ],
)
def test_timestamp(now, expected):
    assert timestamp(now) == expected
    assert HUB_TIMESTAMP.match(expected)


def test_timestamp_of_current_time_has_hub_format():
    assert HUB_TIMESTAMP.match(timestamp(datetime.now(UTC)))


def test_build_note_is_signed_by_the_store_key():
    note = build_note([CONTENT, DATA_CODE, INSTANCE], GATEWAY, KEYPAIR, 1, NOW)
    result = icr.verify_json(note)
    assert result.signature_valid is True
    assert note["signature"]["pubkey"] == KEYPAIR.public_key
    assert note["signature"]["controller"] == CONTROLLER


def test_build_note_fields():
    note = build_note([CONTENT, DATA_CODE, INSTANCE], GATEWAY, KEYPAIR, 1, NOW)
    assert note["$schema"] == NOTE_SCHEMA == "http://purl.org/iscc/schema/iscc-note-0.8.0.json"
    assert note["timestamp"] == "2026-10-09T12:34:56.789Z"
    assert note["gateway"] == GATEWAY
    assert note["units"] == [CONTENT, DATA_CODE]
    assert note["iscc_code"] == ic.gen_iscc_code([CONTENT, DATA_CODE, INSTANCE])["iscc"]


def test_build_note_datahash_is_the_instance_body():
    note = build_note([DATA_CODE, INSTANCE], GATEWAY, KEYPAIR, 1, NOW)
    assert note["datahash"] == BLAKE3_MULTIHASH + ic.iscc_decode(INSTANCE)[4].hex()
    assert note["datahash"].startswith("1e20")
    assert len(note["datahash"]) == 68
    assert note["datahash"] == note["datahash"].lower()


@pytest.mark.parametrize(
    "declared", [[DATA_CODE, INSTANCE], [CONTENT, DATA_CODE, INSTANCE], [SEMANTIC_TEXT, CONTENT, DATA_CODE, INSTANCE]]
)
def test_build_note_iscc_code_reconstructs_from_units_and_datahash(declared):
    note = build_note(declared, GATEWAY, KEYPAIR, 1, NOW)
    rebuilt = ic.gen_iscc_code([*note["units"], instance_from_datahash(note["datahash"])])["iscc"]
    assert rebuilt == note["iscc_code"]
    assert instance_from_datahash(note["datahash"]) == INSTANCE
    assert ic.iscc_decode(note["iscc_code"])[0] == ic.MT.ISCC
    instance_64 = ic.iscc_decode(ic.iscc_decompose(note["iscc_code"])[-1])[4]
    assert instance_64 == bytes.fromhex(note["datahash"][4:])[:8]  # the hub's datahash match


@pytest.mark.parametrize("hub_id", [0, 1, 7, 4095])
def test_build_note_nonce_carries_the_hub_id(hub_id):
    note = build_note([DATA_CODE, INSTANCE], GATEWAY, KEYPAIR, hub_id, NOW)
    assert re.fullmatch(r"[0-9a-f]{32}", note["nonce"])
    assert nonce_hub_id(note["nonce"]) == hub_id


def test_build_note_nonces_are_fresh():
    first = build_note([DATA_CODE, INSTANCE], GATEWAY, KEYPAIR, 1, NOW)
    second = build_note([DATA_CODE, INSTANCE], GATEWAY, KEYPAIR, 1, NOW)
    assert first["nonce"] != second["nonce"]


def test_build_note_rejects_a_hub_id_outside_twelve_bits():
    with pytest.raises(ValueError):
        build_note([DATA_CODE, INSTANCE], GATEWAY, KEYPAIR, 4096, NOW)


def test_build_note_satisfies_hub_field_rules():
    note = build_note([SEMANTIC_TEXT, CONTENT, DATA_CODE, INSTANCE], GATEWAY, KEYPAIR, 1, NOW)
    assert set(note) <= NOTE_FIELDS
    assert set(note["signature"]) <= SIGNATURE_FIELDS
    assert note["signature"]["version"] == "ISCC-SIG v1.0"
    assert 0 < len(note["units"]) <= HUB_MAX_UNITS
    assert all(len(ic.iscc_decode(unit)[4]) == 32 for unit in note["units"])
    assert all(len(value) <= HUB_MAX_STRING for value in note.values() if isinstance(value, str))
    assert len(json.dumps(note).encode()) <= 8192


def test_build_note_with_a_tampered_field_fails_verification():
    note = build_note([DATA_CODE, INSTANCE], GATEWAY, KEYPAIR, 1, NOW)
    note["gateway"] = "https://attacker.example/"
    assert icr.verify_json(note, raise_on_error=False).signature_valid is False


def test_build_note_rejects_units_without_data_code():
    with pytest.raises(ValueError):
        build_note([META, INSTANCE], GATEWAY, KEYPAIR, 1, NOW)


def test_build_delete():
    note = build_delete(ISCC_ID, KEYPAIR, 1, NOW)
    assert note["$schema"] == DELETE_SCHEMA == "http://purl.org/iscc/schema/iscc-note-delete-0.8.0.json"
    assert note["iscc_id"] == ISCC_ID
    assert note["timestamp"] == "2026-10-09T12:34:56.789Z"
    assert nonce_hub_id(note["nonce"]) == 1
    assert set(note) == DELETE_FIELDS
    assert icr.verify_json(note).signature_valid is True
    assert note["signature"]["pubkey"] == KEYPAIR.public_key


def test_build_delete_nonce_carries_the_hub_id():
    assert nonce_hub_id(build_delete(ISCC_ID, KEYPAIR, 4095, NOW)["nonce"]) == 4095


def test_outcome_defaults():
    assert Outcome(ok=False, final=False) == Outcome(ok=False, final=False, iscc_id="", error="")


def note():
    # type: () -> dict
    """A signed note for the hub client tests."""
    return build_note([CONTENT, DATA_CODE, INSTANCE], GATEWAY, KEYPAIR, 1, NOW)


def test_submit_declared():
    seen = []
    signed = note()
    with hub_client(201, seen, json={"iscc_id": ISCC_ID, "seq": 7}) as client:
        outcome = submit(client, HUB_URL, signed)
    assert outcome == Outcome(ok=True, final=True, iscc_id=ISCC_ID)
    (request,) = seen
    assert request.method == "POST"
    assert str(request.url) == f"{HUB_URL}/declaration"
    assert request.headers["X-Force-Declaration"] == "true"
    assert request.headers["Content-Type"] == "application/json"
    assert json.loads(request.content) == signed


@pytest.mark.parametrize(
    ("status", "body", "error"),
    [
        (409, {"error": {"code": "duplicate", "message": "declared before"}}, "HTTP 409 duplicate: declared before"),
        (
            422,
            {"error": {"code": "invalid_iscc", "message": "bad unit", "field": "units"}},
            "HTTP 422 invalid_iscc: bad unit",
        ),
        (
            401,
            {"error": {"code": "invalid_signature", "message": "Invalid signature"}},
            "HTTP 401 invalid_signature: Invalid signature",
        ),
        (400, {"error": {"message": "Nonce hub_id mismatch"}}, "HTTP 400 Nonce hub_id mismatch"),
        (422, {"detail": [{"msg": "field required"}]}, "HTTP 422"),
    ],
)
def test_submit_refused_is_final(status, body, error):
    with hub_client(status, json=body) as client:
        assert submit(client, HUB_URL, note()) == Outcome(ok=False, final=True, error=error)


@pytest.mark.parametrize(
    ("status", "kwargs", "error"),
    [
        (500, {"text": "Internal Server Error"}, "HTTP 500"),
        (502, {"content": b"<html>Bad Gateway</html>"}, "HTTP 502"),
        (
            503,
            {"json": {"error": {"code": "did_unresolvable", "message": "try later"}}},
            "HTTP 503 did_unresolvable: try later",
        ),
        (504, {}, "HTTP 504"),
    ],
)
def test_submit_server_error_is_retryable(status, kwargs, error):
    with hub_client(status, **kwargs) as client:
        assert submit(client, HUB_URL, note()) == Outcome(ok=False, final=False, error=error)


@pytest.mark.parametrize(
    ("handler", "error"), [(unreachable, "ConnectError: hub down"), (timing_out, "ReadTimeout: timed out")]
)
def test_submit_network_error_is_retryable(handler, error):
    with failing_client(handler) as client:
        assert submit(client, HUB_URL, note()) == Outcome(ok=False, final=False, error=error)


@pytest.mark.parametrize("status", [408, 425, 429])
def test_submit_timeout_and_rate_limit_are_retryable(status):
    with hub_client(status, json={"error": {"code": "rate_limited", "message": "slow down"}}) as client:
        outcome = submit(client, HUB_URL, note())
    assert outcome == Outcome(ok=False, final=False, error=f"HTTP {status} rate_limited: slow down")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"text": "Created"},
        {},
        {"json": {"seq": 7}},
        {"json": {"iscc_id": 42}},
        {"json": {"iscc_id": None}},
        {"json": {"iscc_id": ""}},
        {"json": [ISCC_ID]},
        {"json": ISCC_ID},
        {"json": None},
    ],
)
def test_submit_success_without_iscc_id_is_retryable(kwargs):
    with hub_client(201, **kwargs) as client:
        outcome = submit(client, HUB_URL, note())
    assert outcome == Outcome(ok=False, final=False, error="HTTP 201 without an ISCC-ID")


def test_withdraw_deleted():
    seen = []
    signed = build_delete(ISCC_ID, KEYPAIR, 1, NOW)
    with hub_client(204, seen) as client:
        outcome = withdraw(client, HUB_URL, signed)
    assert outcome == Outcome(ok=True, final=True, iscc_id=ISCC_ID)
    (request,) = seen
    assert request.method == "DELETE"
    assert str(request.url) == f"{HUB_URL}/declaration/{ISCC_ID}"
    assert request.url.path == f"/declaration/{ISCC_ID}"
    assert json.loads(request.content) == signed


def test_withdraw_unknown_declaration_counts_as_gone():
    signed = build_delete(ISCC_ID, KEYPAIR, 1, NOW)
    with hub_client(404, json={"error": {"code": "not_found", "message": "Declaration not found"}}) as client:
        assert withdraw(client, HUB_URL, signed) == Outcome(ok=True, final=True, iscc_id=ISCC_ID)


def test_withdraw_refused_is_final():
    signed = build_delete(ISCC_ID, KEYPAIR, 1, NOW)
    body = {"error": {"code": "unauthorized", "message": "Not authorized to delete this declaration"}}
    with hub_client(401, json=body) as client:
        outcome = withdraw(client, HUB_URL, signed)
    assert outcome == Outcome(
        ok=False, final=True, error="HTTP 401 unauthorized: Not authorized to delete this declaration"
    )


def test_withdraw_server_error_is_retryable():
    signed = build_delete(ISCC_ID, KEYPAIR, 1, NOW)
    with hub_client(500, text="oops") as client:
        assert withdraw(client, HUB_URL, signed) == Outcome(ok=False, final=False, error="HTTP 500")


@pytest.mark.parametrize("status", sorted(RETRYABLE))
def test_withdraw_timeout_and_rate_limit_are_retryable(status):
    signed = build_delete(ISCC_ID, KEYPAIR, 1, NOW)
    with hub_client(status, text="later") as client:
        assert withdraw(client, HUB_URL, signed) == Outcome(ok=False, final=False, error=f"HTTP {status}")


def test_withdraw_network_error_is_retryable():
    signed = build_delete(ISCC_ID, KEYPAIR, 1, NOW)
    with failing_client(unreachable) as client:
        assert withdraw(client, HUB_URL, signed) == Outcome(ok=False, final=False, error="ConnectError: hub down")


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"json": {"error": {"code": "invalid_iscc", "message": "refused"}}}, "HTTP 422 invalid_iscc: refused"),
        ({"json": {"error": {"code": "invalid_iscc"}}}, "HTTP 422 invalid_iscc"),
        ({"json": {"error": {"message": "refused"}}}, "HTTP 422 refused"),
        ({"json": {"error": {"code": "", "message": ""}}}, "HTTP 422"),
        ({"json": {"error": {"code": 42, "message": "numeric code"}}}, "HTTP 422 42: numeric code"),
        ({"json": {"error": None}}, "HTTP 422"),
        ({"json": {"error": {}}}, "HTTP 422"),
        ({"json": {"detail": "Unprocessable"}}, "HTTP 422"),
        ({"json": ["not", "an", "object"]}, "HTTP 422"),
        ({"json": "text"}, "HTTP 422"),
        ({"json": None}, "HTTP 422"),
        ({"text": "Unprocessable Entity"}, "HTTP 422"),
        ({"content": b"\xff\xfe"}, "HTTP 422"),
        ({}, "HTTP 422"),
    ],
)
def test_hub_error(kwargs, expected):
    assert hub_error(httpx2.Response(422, **kwargs)) == expected


@pytest.mark.parametrize("error", ["rate limited", ["a", "b"], 42, True, ""])
def test_hub_error_with_non_object_error(error):
    assert hub_error(httpx2.Response(429, json={"error": error})) == "HTTP 429"


def test_retryable_statuses():
    assert {408, 425, 429} == RETRYABLE


@pytest.mark.parametrize(
    ("status", "final"),
    [
        (400, True),
        (401, True),
        (404, True),
        (409, True),
        (422, True),
        (499, True),
        (408, False),
        (425, False),
        (429, False),
        (500, False),
        (503, False),
        (599, False),
    ],
)
def test_is_final(status, final):
    assert is_final(status) is final


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"json": {"iscc_id": ISCC_ID, "seq": 1}}, ISCC_ID),
        ({"json": {"iscc_id": 42}}, ""),
        ({"json": {"iscc_id": ["x"]}}, ""),
        ({"json": {}}, ""),
        ({"json": []}, ""),
        ({"json": "text"}, ""),
        ({"json": None}, ""),
        ({"text": "Created"}, ""),
        ({"content": b"\xff\xfe"}, ""),
        ({}, ""),
    ],
)
def test_issued_id(kwargs, expected):
    assert issued_id(httpx2.Response(201, **kwargs)) == expected
