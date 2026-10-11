"""Tests of reading uploaded C2PA Manifest Stores with c2pa-rs and the acceptance policy, on real signed stores."""

import base64
import datetime as dt
import functools
import io
import json
import time
import uuid

import c2pa
import iscc_core as ic
import pytest
from conftest import (
    C2PA_RS_MANIFEST,
    CONTENT,
    DATA,
    DATA_CODE,
    DIGITAL,
    INSTANCE,
    JPEG,
    VECTOR_1,
    iscc_manifest,
    make_chain,
    sign_manifest,
    signer,
)

from iscc_c2pa_store import manifest
from iscc_c2pa_store.manifest import (
    EXPIRED,
    HARD_BINDING_FAILURES,
    MAX_ID_LENGTH,
    SIGNATURE_VALIDATED,
    TOLERATED_FAILURES,
    UNTRUSTED,
    InvalidManifestError,
    ManifestInfo,
    SoftBinding,
    block_value,
    check_failures,
    check_ingredients,
    claim_generator,
    context,
    read,
    soft_bindings,
)

READER_JSON = c2pa.Reader.json  # the real report, for the test that removes its active manifest
# A real Manifest Store with a CAWG identity assertion (identity claims aggregation), signed by Adobe Content
# Authenticity: the bare JUMBF from the JPEG APP11 segments of the c2pa-rs test fixture C_with_CAWG_data.jpg
# (sdk/tests/fixtures, Apache-2.0 or MIT; copied from c2pa-cpp/tests/fixtures). Its signing certificate has expired
# and its time stamp is not on the trust list, so c2pa-rs reports signingCredential.expired. Its ingredient
# manifest is intact, but its credential is not on the trust list either: an ingredient delta of trust only.
CAWG_MANIFEST = (DATA / "cawg-c2pa-rs.c2pa").read_bytes()
PARENT_TITLE = b"tamper-me-parent.jpg"  # the title of the ingredient manifest of `sign_with_parent`


def report_without_active_manifest(reader):
    # type: (c2pa.Reader) -> str
    """The real c2pa-rs report of a reader with its `active_manifest` removed."""
    report = json.loads(READER_JSON(reader))
    del report["active_manifest"]
    return json.dumps(report)


def report_with_active_label(reader, length):
    # type: (c2pa.Reader, int) -> str
    """The real c2pa-rs report of a reader with its active manifest relabelled to an ID of `length` characters."""
    report = json.loads(READER_JSON(reader))
    label = report["active_manifest"]
    longer = label + "x" * (length - len(label))
    report["manifests"][longer] = report["manifests"].pop(label)
    report["active_manifest"] = longer
    return json.dumps(report)


def sign_labelled(label):
    # type: (str) -> bytes
    """A fresh Manifest Store like `sign_manifest`, whose active manifest has the given label."""
    definition = {
        "claim_generator_info": [{"name": "iscc-c2pa-store-tests", "version": "1.0"}],
        "title": "test.jpg",
        "format": "image/jpeg",
        "label": label,
        "assertions": [
            {"label": "c2pa.actions", "data": {"actions": [{"action": "c2pa.created", "digitalSourceType": DIGITAL}]}}
        ],
    }
    with c2pa.Builder(definition) as builder:
        return builder.sign(signer(), "image/jpeg", io.BytesIO(JPEG.getvalue()), io.BytesIO())


def sign_with_parent():
    # type: () -> bytes
    """A Manifest Store whose parent ingredient is a JPEG with a signed manifest of its own, titled PARENT_TITLE."""
    parent = io.BytesIO()
    actions = {"actions": [{"action": "c2pa.created", "digitalSourceType": DIGITAL}]}
    definition = {"title": PARENT_TITLE.decode(), "assertions": [{"label": "c2pa.actions", "data": actions}]}
    with c2pa.Builder(definition) as builder:
        builder.sign(signer(), "image/jpeg", io.BytesIO(JPEG.getvalue()), parent)
    actions = {"actions": [{"action": "c2pa.opened", "parameters": {"ingredientIds": ["parent"]}}]}
    definition = {"title": "child.jpg", "assertions": [{"label": "c2pa.actions", "data": actions}]}
    ingredient = {"title": "parent.jpg", "relationship": "parentOf", "label": "parent"}
    with c2pa.Builder(definition) as builder:
        parent.seek(0)
        builder.add_ingredient(json.dumps(ingredient), "image/jpeg", parent)
        parent.seek(0)
        return builder.sign(signer(), "image/jpeg", parent, io.BytesIO())


def delta(*codes):
    # type: (str) -> dict
    """An ingredient delta as it appears in a c2pa-rs report, with the given failure codes."""
    failures = [{"code": code, "url": "self#jumbf=/c2pa/urn:c2pa:x"} for code in codes]
    return {
        "ingredientAssertionURI": "self#jumbf=c2pa.assertions/c2pa.ingredient.v3",
        "validationDeltas": {"success": [], "informational": [], "failure": failures},
    }


def extended(data):
    # type: (bytes) -> bytes
    """The same Manifest Store with the extended JUMBF superbox header: LBox 1 and the length in a 64-bit XLBox."""
    return (1).to_bytes(4, "big") + data[4:8] + (len(data) + 8).to_bytes(8, "big") + data[8:]


def refusal(data, trust_anchors=manifest.TRUST_ANCHORS):
    # type: (bytes, object) -> InvalidManifestError
    """The error `read` refuses some bytes with."""
    with pytest.raises(InvalidManifestError) as info:
        read(data, trust_anchors)
    return info.value


def tamper(data, old, new):
    # type: (bytes, bytes, bytes) -> bytes
    """Replace the single occurrence of `old` in a Manifest Store with `new` of the same length."""
    assert data.count(old) == 1
    assert len(old) == len(new)
    return data.replace(old, new)


def status_codes(node):
    # type: (object) -> list[str]
    """Every validation status code anywhere in a c2pa-rs report: the `code` of each object that has one."""
    if isinstance(node, list):
        return [code for item in node for code in status_codes(item)]
    if not isinstance(node, dict):
        return []
    own = [node["code"]] if isinstance(node.get("code"), str) else []
    return own + [code for value in node.values() for code in status_codes(value)]


def assertion(alg, *values, label="c2pa.soft-binding"):
    # type: (object, object, str) -> dict
    """A soft-binding assertion as it appears in a c2pa-rs report, with one block per value."""
    return {"label": label, "data": {"alg": alg, "blocks": [{"scope": {}, "value": value} for value in values]}}


def test_read_accepts_an_iscc_soft_binding():
    seq = [CONTENT, DATA_CODE, INSTANCE]
    info = read(iscc_manifest(seq))
    assert isinstance(info, ManifestInfo)
    assert info.manifest_id.startswith("urn:c2pa:")
    assert info.title == "test.jpg"
    assert info.claim_generator == "iscc-c2pa-store-tests/1.0"
    assert info.signer == "ISCC Test Signer"
    assert info.signed_at == ""  # signed without a time stamp authority
    assert info.trusted is False
    assert info.validation_state == "Invalid"  # the hard binding cannot match without the asset
    assert info.failures == (UNTRUSTED, "assertion.dataHash.mismatch")
    assert info.bindings == (SoftBinding(alg="io.iscc.v0", value=ic.encode_seq(seq), scope={}),)


def test_read_decodes_base64_text_values():
    (binding,) = read(sign_manifest([("io.iscc.v0", VECTOR_1)])).bindings
    assert binding.value == base64.b64decode(VECTOR_1)


def test_read_keeps_the_title():
    assert read(sign_manifest(title="Ünïcode title.jpg")).title == "Ünïcode title.jpg"


def test_read_store_without_soft_binding():
    assert read(sign_manifest()).bindings == ()


def test_read_every_soft_binding_assertion():
    bindings = [("io.iscc.v0", VECTOR_1), ("com.example.watermark", "AAE="), ("io.iscc.v0", "AAI=")]
    info = read(sign_manifest(bindings))
    assert [(b.alg, b.value) for b in info.bindings] == [
        ("io.iscc.v0", base64.b64decode(VECTOR_1)),
        ("com.example.watermark", b"\x00\x01"),
        ("io.iscc.v0", b"\x00\x02"),
    ]


def test_read_drops_undecodable_values():
    info = read(sign_manifest([("com.example.a", "not base64!"), ("com.example.b", ""), ("com.example.c", "AAE=")]))
    assert info.bindings == (SoftBinding(alg="com.example.c", value=b"\x00\x01", scope={}),)


def test_read_drops_non_ascii_values():
    info = read(sign_manifest([("com.example.a", "ü"), ("com.example.b", "AAE=")]))
    assert info.bindings == (SoftBinding(alg="com.example.b", value=b"\x00\x01", scope={}),)


def test_read_trusted_store(trust_anchors):
    info = read(iscc_manifest(), trust_anchors)
    assert info.trusted is True
    assert info.failures == ("assertion.dataHash.mismatch",)


def test_read_c2pa_rs_fixture():
    info = read(C2PA_RS_MANIFEST)
    assert info == ManifestInfo(
        manifest_id="contentauth:urn:uuid:b2b1f7fa-b119-4de1-9c0d-c97fbea3f2c3",
        title="C.jpg",
        media_type="image/jpeg",
        claim_generator="make_test_images/0.33.1 c2pa-rs/0.33.1",
        signer="C2PA Signer",
        signed_at="2024-08-06T21:53:37+00:00",
        trusted=False,
        validation_state="Invalid",
        failures=(UNTRUSTED, "assertion.dataHash.mismatch"),
        bindings=(),
    )


def test_read_c2pa_rs_fixture_stays_untrusted_with_other_anchors(trust_anchors):
    assert read(C2PA_RS_MANIFEST, trust_anchors).trusted is False


def test_cawg_fixture_carries_an_identity_assertion():
    report = manifest.parse(CAWG_MANIFEST, manifest.TRUST_ANCHORS)
    active = report["manifests"][report["active_manifest"]]
    assert "cawg.identity" in [item["label"] for item in active["assertions"]]


def test_cawg_identity_assertions_are_not_evaluated():
    # with identity decoding on, c2pa-rs reports cawg.ica.did_unavailable for this store, or fetches the issuer's
    # did:web document when network access is allowed and reports cawg.ica.untrusted_issuer
    codes = status_codes(manifest.parse(CAWG_MANIFEST, manifest.TRUST_ANCHORS))
    assert SIGNATURE_VALIDATED in codes
    assert [code for code in codes if code.startswith("cawg.")] == []


def test_cawg_identity_assertions_are_not_evaluated_with_other_anchors(trust_anchors):
    codes = status_codes(manifest.parse(CAWG_MANIFEST, trust_anchors))
    assert [code for code in codes if code.startswith("cawg.")] == []


def test_read_accepts_cawg_fixture_with_its_expired_signer_as_untrusted():
    info = read(CAWG_MANIFEST)
    assert info.trusted is False
    assert info.failures == (UNTRUSTED, EXPIRED, "assertion.dataHash.mismatch")
    assert info.signed_at == "2025-05-30T20:15:55+00:00"  # from a time stamp that is not on the trust list


def test_read_accepts_cawg_fixture_with_its_untrusted_ingredient():
    report = manifest.parse(CAWG_MANIFEST, manifest.TRUST_ANCHORS)
    (ingredient,) = report["validation_results"]["ingredientDeltas"]
    assert [item["code"] for item in ingredient["validationDeltas"]["failure"]] == [UNTRUSTED]
    assert read(CAWG_MANIFEST).manifest_id == report["active_manifest"]


def test_read_records_an_expired_trusted_credential_as_untrusted(tmp_path):
    # the signing certificate expires within three seconds, so the store is signed and read while it is valid, and
    # read again after it expired
    chain = make_chain(dt.datetime.now(dt.UTC) - dt.timedelta(days=30, seconds=-3))
    data = sign_manifest(chain=chain)
    anchors = tmp_path / "trust.pem"
    anchors.write_text(chain.ca_pem, encoding="utf-8")
    assert read(data, anchors).trusted is True
    time.sleep(4)
    info = read(data, anchors)
    assert info.trusted is False
    assert info.failures == (UNTRUSTED, EXPIRED, "assertion.dataHash.mismatch")


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"hello world" * 10,
        b"\x00" * 64,
        b"jumb",
    ],
)
def test_read_refuses_unreadable_bytes(data):
    error = refusal(data)
    assert error.code == "manifest.unreadable"
    assert error.message
    assert str(error) == f"manifest.unreadable: {error.message}"


@pytest.mark.parametrize("keep", [8, 16, 1000, 0.5, 0.99])
def test_read_refuses_truncated_store(keep):
    data = iscc_manifest()
    cut = keep if isinstance(keep, int) else int(len(data) * keep)
    assert refusal(data[:cut]).code == "manifest.unreadable"


@pytest.mark.parametrize(
    "tail",
    [b"\x00", b"<html>anything</html>", bytes(range(256)) * 4096, C2PA_RS_MANIFEST],
    ids=["byte", "html", "megabyte", "second-store"],
)
def test_read_refuses_bytes_after_the_store(tail):
    # c2pa-rs alone reads the first JUMBF superbox and ignores the tail
    assert manifest.parse(C2PA_RS_MANIFEST + tail, manifest.TRUST_ANCHORS)["active_manifest"]
    error = refusal(C2PA_RS_MANIFEST + tail)
    assert error.code == "manifest.trailingData"
    assert error.message == "The upload must be the Manifest Store alone, as one JUMBF superbox"


def test_read_accepts_an_extended_box_length():
    info = read(extended(C2PA_RS_MANIFEST))
    assert info == read(C2PA_RS_MANIFEST)


def test_read_refuses_bytes_after_a_store_with_extended_box_length():
    data = extended(C2PA_RS_MANIFEST) + b"\x00"
    assert manifest.parse(data, manifest.TRUST_ANCHORS)["active_manifest"]
    assert refusal(data).code == "manifest.trailingData"


def test_read_refuses_a_box_that_extends_to_the_end():
    # LBox 0 (the box ends with the file) is valid JUMBF, but c2pa-rs does not read it
    assert refusal(b"\x00\x00\x00\x00" + C2PA_RS_MANIFEST[4:]).code == "manifest.unreadable"


def test_read_accepts_an_intact_ingredient_manifest():
    data = sign_with_parent()
    assert data.count(PARENT_TITLE) == 1  # in the claim of the ingredient manifest only
    assert read(data).title == "child.jpg"


def test_read_refuses_a_tampered_ingredient_manifest():
    error = refusal(tamper(sign_with_parent(), PARENT_TITLE, PARENT_TITLE.upper()))
    assert error.code == "ingredient.manifest.mismatch"
    assert error.message == "An ingredient manifest failed validation"


@pytest.mark.parametrize(
    "deltas",
    [
        [],
        [delta()],
        [delta(UNTRUSTED, EXPIRED, "signingCredential.ocsp.skipped")],
        [delta(UNTRUSTED), delta(EXPIRED)],
        [{"ingredientAssertionURI": "self#jumbf=c2pa.assertions/c2pa.ingredient.v3"}],
    ],
)
def test_check_ingredients_tolerates_differences_of_trust(deltas):
    assert check_ingredients(deltas) is None


@pytest.mark.parametrize(
    ("deltas", "code"),
    [
        ([delta("ingredient.manifest.mismatch", "claimSignature.mismatch")], "ingredient.manifest.mismatch"),
        ([delta(UNTRUSTED), delta(EXPIRED, "assertion.hashedURI.mismatch")], "assertion.hashedURI.mismatch"),
        ([delta("timeStamp.mismatch")], "timeStamp.mismatch"),
    ],
)
def test_check_ingredients_refuses_mismatches(deltas, code):
    with pytest.raises(InvalidManifestError) as info:
        check_ingredients(deltas)
    assert info.value.code == code


def test_read_refuses_tampered_soft_binding_algorithm():
    data = tamper(sign_manifest([("io.iscc.v0", VECTOR_1)]), b"io.iscc.v0", b"io.iscc.v1")
    error = refusal(data)
    assert error.code == "assertion.hashedURI.mismatch"
    assert error.message == "The active manifest failed validation"


def test_read_refuses_tampered_soft_binding_value():
    old = VECTOR_1[:24].encode()
    data = tamper(sign_manifest([("io.iscc.v0", VECTOR_1)]), old, b"B" + old[1:])
    assert refusal(data).code == "assertion.hashedURI.mismatch"


def test_read_refuses_tampered_claim():
    data = tamper(sign_manifest(title="tamper-me.jpg"), b"tamper-me", b"tamper-mf")
    assert refusal(data).code == "claimSignature.mismatch"


def test_read_refuses_tampered_claim_of_trusted_store(trust_anchors):
    data = tamper(sign_manifest(title="tamper-me.jpg"), b"tamper-me", b"tamper-mf")
    assert refusal(data, trust_anchors).code == "claimSignature.mismatch"


def test_read_refuses_report_without_active_manifest(monkeypatch):
    # c2pa-rs always names an active manifest for readable bytes, so its real report is edited here
    monkeypatch.setattr(c2pa.Reader, "json", report_without_active_manifest)
    error = refusal(iscc_manifest())
    assert error.code == "manifest.missing"


def test_read_keeps_a_label_with_claim_generator_identifier():
    label = f"urn:c2pa:{uuid.uuid4()}:iscc-tests:1_1"
    # c2pa-rs drops the version and reason from the label of a first claim
    assert read(sign_labelled(label)).manifest_id == label.removesuffix(":1_1")


def test_read_refuses_a_label_that_is_not_printable():
    # c2pa-rs signs and reads a DEL character in the claim generator identifier of the label
    error = refusal(sign_labelled(f"urn:c2pa:{uuid.uuid4()}:iscc\x7ftests:1_1"))
    assert error.code == "manifest.idInvalid"
    assert error.message == f"The manifest ID must be printable, at most {MAX_ID_LENGTH} characters"


def test_read_accepts_the_longest_label(monkeypatch):
    # c2pa-rs limits the labels it signs far below MAX_ID_LENGTH, so its real report is relabelled here
    monkeypatch.setattr(c2pa.Reader, "json", functools.partialmethod(report_with_active_label, MAX_ID_LENGTH))
    info = read(iscc_manifest())
    assert len(info.manifest_id) == MAX_ID_LENGTH
    assert info.manifest_id.startswith("urn:c2pa:")


def test_read_refuses_an_overlong_label(monkeypatch):
    monkeypatch.setattr(c2pa.Reader, "json", functools.partialmethod(report_with_active_label, MAX_ID_LENGTH + 1))
    assert refusal(iscc_manifest()).code == "manifest.idInvalid"


def test_invalid_manifest_error_is_value_error():
    error = InvalidManifestError("some.code", "Some message")
    assert isinstance(error, ValueError)
    assert (error.code, error.message, str(error)) == ("some.code", "Some message", "some.code: Some message")


def test_context_is_cached_per_trust_list(trust_anchors):
    assert context(trust_anchors) is context(trust_anchors)
    assert context() is context()
    assert context(trust_anchors) is not context()


def test_tolerated_failures():
    assert UNTRUSTED in TOLERATED_FAILURES
    assert EXPIRED in TOLERATED_FAILURES
    assert "signingCredential.revoked" not in TOLERATED_FAILURES
    assert HARD_BINDING_FAILURES < TOLERATED_FAILURES
    assert "assertion.hashedURI.mismatch" not in TOLERATED_FAILURES


@pytest.mark.parametrize(
    "failures",
    [
        [],
        [UNTRUSTED],
        sorted(HARD_BINDING_FAILURES),
        [UNTRUSTED, "assertion.bmffHash.mismatch"],
        [UNTRUSTED, EXPIRED, "assertion.dataHash.mismatch"],
    ],
)
def test_check_failures_accepts_tolerated_failures(failures):
    assert check_failures(failures, ["claimSignature.insideValidity", SIGNATURE_VALIDATED]) is None


@pytest.mark.parametrize(
    ("failures", "code"),
    [
        (["claimSignature.mismatch"], "claimSignature.mismatch"),
        ([UNTRUSTED, "assertion.hashedURI.mismatch", "claim.malformed"], "assertion.hashedURI.mismatch"),
        (["assertion.dataHash.mismatch", "signingCredential.revoked"], "signingCredential.revoked"),
        ([EXPIRED, "signingCredential.invalid"], "signingCredential.invalid"),
    ],
)
def test_check_failures_refuses_with_the_first_intolerable_code(failures, code):
    with pytest.raises(InvalidManifestError) as info:
        check_failures(failures, [SIGNATURE_VALIDATED])
    assert info.value.code == code


@pytest.mark.parametrize("successes", [[], ["claimSignature.insideValidity", "assertion.hashedURI.match"]])
def test_check_failures_requires_a_validated_claim_signature(successes):
    with pytest.raises(InvalidManifestError) as info:
        check_failures([UNTRUSTED], successes)
    assert info.value.code == "claimSignature.missing"


@pytest.mark.parametrize(
    ("claim", "expected"),
    [
        ({"claim_generator": "make_test_images/0.33.1 c2pa-rs/0.33.1"}, "make_test_images/0.33.1 c2pa-rs/0.33.1"),
        (
            {"claim_generator": "v1", "claim_generator_info": [{"name": "v2", "version": "1"}]},
            "v1",
        ),
        ({"claim_generator": "", "claim_generator_info": [{"name": "app", "version": "2.0"}]}, "app/2.0"),
        ({"claim_generator_info": [{"name": "app", "version": "2.0", "org.contentauth.c2pa_rs": "0.91"}]}, "app/2.0"),
        ({"claim_generator_info": [{"name": "app"}]}, "app"),
        ({"claim_generator_info": [{"version": "2.0"}]}, "/2.0"),
        ({"claim_generator_info": [{"name": "a", "version": "1"}, {"name": "b"}]}, "a/1 b"),
        ({"claim_generator_info": [{"name": "app", "version": 3}]}, "app/3"),
        ({"claim_generator_info": []}, ""),
        ({"claim_generator_info": None}, ""),
        ({}, ""),
    ],
)
def test_claim_generator(claim, expected):
    assert claim_generator(claim) == expected


def test_soft_bindings_of_every_block():
    found = soft_bindings([assertion("io.iscc.v0", "AAE=", [0, 2], "AAM=")])
    assert [b.value for b in found] == [b"\x00\x01", b"\x00\x02", b"\x00\x03"]


def test_soft_bindings_keep_the_scope():
    block = {"scope": {"region": [{"type": "temporal"}]}, "value": "AAE="}
    (found,) = soft_bindings([{"label": "c2pa.soft-binding", "data": {"alg": "x.y", "blocks": [block]}}])
    assert found == SoftBinding(alg="x.y", value=b"\x00\x01", scope={"region": [{"type": "temporal"}]})


def test_soft_bindings_default_scope():
    blocks = [{"value": "AAE="}, {"scope": None, "value": "AAE="}]
    found = soft_bindings([{"label": "c2pa.soft-binding", "data": {"alg": "x.y", "blocks": blocks}}])
    assert [b.scope for b in found] == [{}, {}]


@pytest.mark.parametrize(
    ("label", "kept"),
    [
        ("c2pa.soft-binding", True),
        ("c2pa.soft-binding__1", True),
        ("c2pa.soft-binding__12", True),
        ("c2pa.soft-bindings", False),
        ("c2pa.soft-binding_1", False),
        ("c2pa.hash.data", False),
        ("com.example.soft-binding", False),
        ("", False),
    ],
)
def test_soft_bindings_by_label(label, kept):
    assert bool(soft_bindings([assertion("x.y", "AAE=", label=label)])) is kept


def test_soft_bindings_without_label():
    assert soft_bindings([{"data": {"alg": "x.y", "blocks": [{"value": "AAE="}]}}]) == []


@pytest.mark.parametrize("alg", [None, "", 42, ["io.iscc.v0"]])
def test_soft_bindings_need_an_algorithm(alg):
    assert soft_bindings([assertion(alg, "AAE=")]) == []


def test_soft_bindings_missing_alg():
    data = {"blocks": [{"scope": {}, "value": "AAE="}]}
    assert soft_bindings([{"label": "c2pa.soft-binding", "data": data}]) == []


@pytest.mark.parametrize(
    "data",
    [None, {}, {"alg": "x.y"}, {"alg": "x.y", "blocks": None}, {"alg": "x.y", "blocks": []}],
)
def test_soft_bindings_without_blocks(data):
    assert soft_bindings([{"label": "c2pa.soft-binding", "data": data}]) == []


def test_soft_bindings_drop_empty_and_undecodable_values():
    found = soft_bindings([assertion("x.y", "", "!!", [], [300], None, {"a": 1}, "AAE=")])
    assert [b.value for b in found] == [b"\x00\x01"]


def test_soft_bindings_across_assertions_in_order():
    found = soft_bindings(
        [
            {"label": "c2pa.actions.v2", "data": {"actions": []}},
            assertion("a.a", "AAE="),
            assertion("b.b", "AAI=", label="c2pa.soft-binding__1"),
        ]
    )
    assert [(b.alg, b.value) for b in found] == [("a.a", b"\x00\x01"), ("b.b", b"\x00\x02")]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ([0, 1, 255], b"\x00\x01\xff"),
        ([], b""),
        ([256], b""),
        ([-1], b""),
        ([1, "2"], b""),
        ([1.0], b""),
        ([[1]], b""),
        ("AAE=", b"\x00\x01"),
        ("AAE", b""),  # base64 text from c2pa-rs is padded
        ("AA-_", b""),  # and in the standard alphabet
        ("not base64!", b""),
        ("", b""),
        (None, b""),
        (42, b""),
        ({"value": "AAE="}, b""),
        (b"\x00\x01", b""),
    ],
)
def test_block_value(value, expected):
    assert block_value(value) == expected


@pytest.mark.parametrize("value", ["ü", "٠١٢٣", "AAE=ü", "ÄÖÜ="])
def test_block_value_of_non_ascii_text(value):
    assert block_value(value) == b""
