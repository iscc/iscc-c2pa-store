"""Soft binding values: lenient base64 decoding for every algorithm, and ISCC-SEQ decoding for `io.iscc.v0`.

An `io.iscc.v0` value is an ISCC-SEQ (IEP-0020). Only 256-bit Version 0 Semantic-, Content-, Data- and Instance-Codes
are searchable: IEP-0020 requires resolvers to ignore shorter units, and equal metadata (Meta-Code) says nothing about
equal content.
"""

import base64
import binascii
from dataclasses import dataclass

import iscc_core as ic

ISCC_ALG = "io.iscc.v0"
MAX_VALUE_LENGTH = 4096  # base64 characters of a query value, about 90 ISCC-UNITs of 256 bits
MAX_VALUE_BYTES = MAX_VALUE_LENGTH * 3 // 4  # longest stored block value
SEARCHABLE_TYPES = {ic.MT.SEMANTIC, ic.MT.CONTENT, ic.MT.DATA, ic.MT.INSTANCE}
SIMILARITY_TYPES = {ic.MT.SEMANTIC, ic.MT.CONTENT, ic.MT.DATA}  # compared by Hamming distance; Instance by equality


@dataclass(frozen=True)
class Unit:
    """A searchable ISCC-UNIT: its canonical string, its type key and its 32-byte body."""

    iscc: str
    unit_type: str  # MainType, SubType and Version, e.g. "CONTENT_IMAGE_V0"; only equal types are comparable
    maintype: ic.MT
    body: bytes


def decode_base64(value):
    # type: (str) -> bytes
    """Decode base64 leniently: standard or URL-safe alphabet, with or without padding.

    A `+` that arrives as a space because the client did not percent-encode the query is restored, also at the start
    or end of the value; other surrounding whitespace is ignored.

    :raises ValueError: if the value is longer than MAX_VALUE_LENGTH or not base64.
    """
    if len(value) > MAX_VALUE_LENGTH:
        raise ValueError(f"Value exceeds {MAX_VALUE_LENGTH} characters")
    # A space can only be an unencoded "+", so it is restored before other surrounding whitespace is trimmed.
    text = value.replace(" ", "+").strip("\t\n\r\f\v").replace("-", "+").replace("_", "/").rstrip("=")
    text += "=" * (-len(text) % 4)
    try:
        return base64.b64decode(text, validate=True)
    except binascii.Error as e:
        raise ValueError("Value is not valid base64") from e


def unit_type(maintype, subtype, version):
    # type: (int, int, int) -> str
    """Name the comparable type of an ISCC-UNIT, e.g. `CONTENT_IMAGE_V0`."""
    subtypes = ic.SUBTYPE_MAP[(ic.MT(maintype), ic.VS(version))]
    return f"{ic.MT(maintype).name}_{subtypes(subtype).name}_{ic.VS(version).name}"


def searchable(iscc):
    # type: (str) -> Unit | None
    """Return the ISCC-UNIT as a searchable Unit, or None if it is not a 256-bit Version 0 S-, C-, D- or I-Code.

    Units with a SubType unknown to iscc-core are ignored, as IEP-0020 requires, instead of failing.
    """
    try:
        maintype, subtype, version, _length, body = ic.iscc_decode(iscc)
    except ValueError:
        return None
    if maintype not in SEARCHABLE_TYPES or version != ic.VS.V0 or len(body) != 32:
        return None
    try:
        kind = unit_type(maintype, subtype, version)
    except ValueError:  # a SubType iscc-core does not know
        return None
    return Unit(iscc=ic.iscc_normalize(iscc), unit_type=kind, maintype=ic.MT(maintype), body=body)


def decode_units(seq):
    # type: (bytes) -> list[Unit]
    """Decode the raw bytes of an `io.iscc.v0` value into its searchable units, deduplicated, in input order.

    :raises ValueError: if the bytes are not a valid ISCC-SEQ or hold no searchable unit.
    """
    units = [searchable(iscc) for iscc in dict.fromkeys(ic.decode_seq(seq))]
    found = [unit for unit in units if unit is not None]
    if not found:
        raise ValueError("Value holds no 256-bit Version 0 Semantic-, Content-, Data- or Instance-Code")
    return found
