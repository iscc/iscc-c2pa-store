"""Tests of soft binding value decoding: lenient base64 and ISCC-SEQ (IEP-0020) to searchable units."""

import base64

import iscc_core as ic
import iscc_core.codec
import pytest
from conftest import CONTENT, DATA_CODE, INSTANCE, META, VECTOR_1, encode, flip

from iscc_c2pa_store.binding import (
    MAX_VALUE_BYTES,
    MAX_VALUE_LENGTH,
    Unit,
    decode_base64,
    decode_units,
    searchable,
    unit_type,
)

# IEP-0020 Vector 2: 64- and 128-bit Content-Codes next to a 256-bit Data-Code
VECTOR_2 = (
    "21018c3585376ee22d5a22039f07f3ff9e0061ff3ef7cbffffef37ff300705b41f3472493225d27edcbdb63236846a53ee28"
    "deca5e3561acc0c9bbdf81b5"
)
UNKNOWN_SUBTYPE = bytes.fromhex("270000000000")  # CONTENT SubType 7, accepted by IEP-0020 decoders


def isccs(units):
    # type: (list[Unit]) -> list[str]
    """The canonical ISCC strings of decoded units."""
    return [unit.iscc for unit in units]


def test_decode_vector_1_drops_meta_code():
    assert isccs(decode_units(decode_base64(VECTOR_1))) == [CONTENT, DATA_CODE, INSTANCE]


def test_encode_matches_reference_vector():
    assert encode([META, CONTENT, DATA_CODE, INSTANCE]) == VECTOR_1


def test_decoded_units_carry_type_and_body():
    content, data, instance = decode_units(decode_base64(VECTOR_1))
    assert content == Unit(
        iscc=CONTENT, unit_type="CONTENT_TEXT_V0", maintype=ic.MT.CONTENT, body=ic.iscc_decode(CONTENT)[4]
    )
    assert (data.unit_type, data.maintype) == ("DATA_NONE_V0", ic.MT.DATA)
    assert (instance.unit_type, instance.maintype) == ("INSTANCE_NONE_V0", ic.MT.INSTANCE)
    assert all(len(unit.body) == 32 for unit in (content, data, instance))


@pytest.mark.parametrize(
    "variant",
    [
        VECTOR_1.rstrip("="),  # unpadded
        VECTOR_1.replace("+", "-").replace("/", "_"),  # URL-safe alphabet
        VECTOR_1.replace("+", "-").replace("/", "_").rstrip("="),  # URL-safe and unpadded
        VECTOR_1.replace("+", " "),  # '+' decoded as space by a client that did not percent-encode
        f"\t{VECTOR_1}\r\n",  # surrounding whitespace other than spaces
        f"\f\v{VECTOR_1}\n\n",
        VECTOR_1 + "==",  # surplus padding
    ],
)
def test_decode_base64_is_lenient(variant):
    assert decode_base64(variant) == base64.b64decode(VECTOR_1)


@pytest.mark.parametrize(
    ("value", "plus"),
    [
        (" AAA", "+AAA"),  # a leading space is an unencoded "+"
        ("AA ", "AA+"),  # and so is a trailing one
        ("\t AA A \n", "+AA+A+"),  # spaces turn into "+" before other whitespace is stripped
        ("  " + VECTOR_1, "++" + VECTOR_1),
    ],
)
def test_decode_base64_turns_every_space_into_plus(value, plus):
    assert decode_base64(value) == decode_base64(plus)


def test_decode_base64_surrounding_spaces_change_the_value():
    assert decode_base64(" AAA") == b"\xf8\x00\x00" != decode_base64("AAA")


def test_decode_base64_restores_padding():
    assert decode_base64("AAE") == b"\x00\x01"
    assert decode_base64("AA") == b"\x00"


def test_decode_base64_of_empty_value():
    assert decode_base64("") == b""


def test_decode_base64_accepts_the_longest_value():
    assert decode_base64("A" * MAX_VALUE_LENGTH) == bytes(MAX_VALUE_BYTES)


def test_decode_base64_rejects_overlong_value():
    with pytest.raises(ValueError, match="exceeds"):
        decode_base64("A" * (MAX_VALUE_LENGTH + 1))


@pytest.mark.parametrize("value", ["not*base64", "A", "AAAAA", "AA=A", "ab$c"])
def test_decode_base64_rejects_invalid_base64(value):
    with pytest.raises(ValueError, match="base64"):
        decode_base64(value)


def test_decode_base64_rejects_non_ascii():
    with pytest.raises(ValueError):
        decode_base64("ÄÖÜ=")


def test_decode_units_removes_duplicate_units():
    assert isccs(decode_units(ic.encode_seq([DATA_CODE, INSTANCE, DATA_CODE]))) == [DATA_CODE, INSTANCE]


def test_decode_units_keeps_input_order():
    assert isccs(decode_units(ic.encode_seq([INSTANCE, DATA_CODE, CONTENT]))) == [INSTANCE, DATA_CODE, CONTENT]


def test_decode_units_keeps_similar_units_of_the_same_type():
    similar = flip(CONTENT, 8)
    assert isccs(decode_units(ic.encode_seq([CONTENT, similar]))) == [CONTENT, similar]


@pytest.mark.parametrize(
    "hex_bytes",
    [
        "",  # no ISCC-UNIT
        "28000100000000",  # nonzero header padding nibble
        "200800" + "00" * 36,  # Length field 8
        "3000000000",  # truncated ISCC-BODY
        "30000000000000",  # trailing byte after a complete ISCC-UNIT
        "5005578c7c8fbfbdb58fa88248c53a852c8d05b41f3472493225a522af4a2c9cbf6a",  # ISCC-CODE
        "60100000000000000000",  # ISCC-ID
    ],
)
def test_decode_units_rejects_iep_0020_reject_vectors(hex_bytes):
    with pytest.raises(ValueError):
        decode_units(bytes.fromhex(hex_bytes))


def test_decode_units_drops_short_units():
    assert isccs(decode_units(bytes.fromhex(VECTOR_2))) == [DATA_CODE]


def test_decode_units_ignores_unknown_subtype_next_to_valid_units():
    assert isccs(decode_units(UNKNOWN_SUBTYPE + ic.encode_seq([DATA_CODE]))) == [DATA_CODE]


def test_decode_units_rejects_unknown_subtype_unit_alone():
    with pytest.raises(ValueError, match="256-bit"):
        decode_units(UNKNOWN_SUBTYPE)


def test_decode_units_rejects_meta_code_only():
    with pytest.raises(ValueError, match="256-bit"):
        decode_units(ic.encode_seq([META]))


def test_decode_units_normalizes_to_canonical_iscc():
    (unit,) = decode_units(ic.encode_seq([DATA_CODE]))
    assert unit.iscc.startswith("ISCC:")
    assert unit.iscc == ic.iscc_normalize(DATA_CODE)


SEMANTIC_IMAGE = "ISCC:" + ic.encode_component(ic.MT.SEMANTIC, ic.ST_CC.IMAGE, ic.VS.V0, 256, bytes(range(32)))
CONTENT_128 = "ISCC:" + ic.encode_component(ic.MT.CONTENT, ic.ST_CC.TEXT, ic.VS.V0, 128, bytes(16))
FLAKE = "ISCC:" + ic.encode_component(ic.MT.FLAKE, ic.ST.NONE, ic.VS.V0, 64, bytes(8))
# A 256-bit Data-Code of Version 1, which iscc-core decodes but has no SubType table for
DATA_V1 = ic.encode_header(ic.MT.DATA, ic.ST.NONE, ic.VS.V1, 7) + bytes(32)


@pytest.mark.parametrize(
    ("iscc", "expected"),
    [
        (CONTENT, "CONTENT_TEXT_V0"),
        (DATA_CODE, "DATA_NONE_V0"),
        (INSTANCE, "INSTANCE_NONE_V0"),
        (SEMANTIC_IMAGE, "SEMANTIC_IMAGE_V0"),
        (META, None),  # Meta-Codes say nothing about equal content
        (CONTENT_128, None),  # shorter than 256 bits
        ("ISCC:EAASKDNZNYGUUF5A", None),  # 64-bit Content-Code
        (FLAKE, None),  # not a content unit
        ("ISCC:KUAALNA7GRZESMRFUURK6SRMTS7WU", None),  # ISCC-CODE
        ("not an iscc", None),
    ],
)
def test_searchable(iscc, expected):
    unit = searchable(iscc)
    assert (unit.unit_type if unit else None) == expected


def test_searchable_accepts_iscc_without_prefix():
    unit = searchable(CONTENT.removeprefix("ISCC:"))
    assert unit is not None
    assert unit.iscc == CONTENT


@pytest.mark.parametrize(
    ("maintype", "subtype", "version", "expected"),
    [
        (ic.MT.CONTENT, ic.ST_CC.IMAGE, ic.VS.V0, "CONTENT_IMAGE_V0"),
        (ic.MT.CONTENT, ic.ST_CC.MIXED, ic.VS.V0, "CONTENT_MIXED_V0"),
        (ic.MT.SEMANTIC, ic.ST_CC.TEXT, ic.VS.V0, "SEMANTIC_TEXT_V0"),
        (ic.MT.DATA, ic.ST.NONE, ic.VS.V0, "DATA_NONE_V0"),
        (ic.MT.INSTANCE, ic.ST.NONE, ic.VS.V0, "INSTANCE_NONE_V0"),
        (2, 3, 0, "CONTENT_VIDEO_V0"),  # plain integers from a decoded header
    ],
)
def test_unit_type(maintype, subtype, version, expected):
    assert unit_type(maintype, subtype, version) == expected


@pytest.mark.parametrize(("maintype", "subtype", "version"), [(2, 7, 0), (3, 0, 1), (9, 0, 0)])
def test_unit_type_rejects_unknown_types(maintype, subtype, version):
    with pytest.raises((ValueError, KeyError)):
        unit_type(maintype, subtype, version)


def test_searchable_ignores_unknown_version():
    assert searchable("ISCC:" + ic.encode_base32(DATA_V1)) is None


def test_decode_units_ignores_unknown_version_next_to_valid_units():
    assert isccs(decode_units(DATA_V1 + ic.encode_seq([DATA_CODE]))) == [DATA_CODE]


# A 256-bit Content-Code of SubType 5, which iscc-core has no SubType for
CONTENT_SUBTYPE_5 = "ISCC:" + ic.encode_base32(ic.encode_header(ic.MT.CONTENT, 5, ic.VS.V0, 7) + bytes(32))


def accept_prefix(monkeypatch, prefix):
    # type: (pytest.MonkeyPatch, str) -> None
    """Make iscc-core decode ISCCs with a prefix it does not know, as a decoder ahead of its SubType table would."""
    monkeypatch.setattr(iscc_core.codec, "PREFIXES", [*iscc_core.codec.PREFIXES, prefix])


def test_searchable_ignores_unknown_subtype():
    assert searchable(CONTENT_SUBTYPE_5) is None


def test_searchable_ignores_unknown_subtype_that_decodes(monkeypatch):
    # iscc-core refuses the prefix of every SubType it cannot name; a decoder that accepts one must not fail us
    accept_prefix(monkeypatch, "EU")
    assert ic.iscc_decode(CONTENT_SUBTYPE_5)[:4] == (ic.MT.CONTENT, 5, ic.VS.V0, 7)
    assert searchable(CONTENT_SUBTYPE_5) is None


def test_decode_units_ignores_unknown_subtype_that_decodes(monkeypatch):
    accept_prefix(monkeypatch, "EU")
    seq = ic.decode_base32(CONTENT_SUBTYPE_5.removeprefix("ISCC:")) + ic.encode_seq([DATA_CODE])
    assert isccs(decode_units(seq)) == [DATA_CODE]


@pytest.mark.parametrize(
    "iscc",
    [
        "ISCC:" + ic.encode_component(ic.MT.META, ic.ST.NONE, ic.VS.V0, 256, bytes(32)),
        "ISCC:" + ic.encode_base32(DATA_V1),
        "ISCC:" + ic.encode_component(ic.MT.DATA, ic.ST.NONE, ic.VS.V0, 128, bytes(16)),
    ],
)
def test_searchable_checks_type_version_and_length_first(iscc):
    assert searchable(iscc) is None
