"""Property tests of the code that handles untrusted input: it fails only with the errors its callers handle.

Examples are derandomized, so every run explores the same inputs and the suite stays reproducible.
"""

import base64
import functools

import iscc_core as ic
from conftest import CONTENT, DATA_CODE, INSTANCE, META, iscc_manifest
from hypothesis import example, given, settings
from hypothesis import strategies as st

from iscc_c2pa_store import binding, manifest, store

PROPERTY = settings(max_examples=200, deadline=None, database=None, derandomize=True)
# The algorithm the store decodes (three times as likely, to reach its block limit), another one on the C2PA list,
# and one that is not on it
ALGS = [binding.ISCC_ALG, binding.ISCC_ALG, binding.ISCC_ALG, "com.adobe.trustmark.Q", "com.example.unlisted"]
SCOPES = [{}, {"region": [{"type": "temporal"}]}]
OVERLONG = bytes(binding.MAX_VALUE_BYTES + 1)
# 36 distinct 256-bit Data-Codes in 6 whole-asset io.iscc.v0 blocks: more units than the store indexes
MANY_UNITS = [
    manifest.SoftBinding(
        alg=binding.ISCC_ALG,
        value=b"".join(
            ic.encode_header(ic.MT.DATA, ic.ST.NONE, ic.VS.V0, 7) + (6 * block + i).to_bytes(32, "big")
            for i in range(6)
        ),
        scope={},
    )
    for block in range(6)
]
BASE64_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/-_= \n"
KNOWN_UNITS = [META, CONTENT, DATA_CODE, INSTANCE]
JSON_SCALARS = st.none() | st.booleans() | st.integers() | st.floats(allow_nan=False)


def containers(text, children):
    # type: (st.SearchStrategy, st.SearchStrategy) -> st.SearchStrategy
    """JSON arrays and objects of `children`, with keys drawn from `text`."""
    return st.lists(children, max_size=4) | st.dictionaries(text, children, max_size=4)


def json_values(text):
    # type: (st.SearchStrategy) -> st.SearchStrategy
    """JSON-like values as c2pa-rs could report them, with strings drawn from `text`."""
    return st.recursive(JSON_SCALARS | text, functools.partial(containers, text), max_leaves=10)


@st.composite
def unit_bytes(draw, versions=(0, 1)):
    # type: (object, tuple[int, ...]) -> bytes
    """One ISCC-UNIT in binary: any MainType, SubType, Version and Length, with a body of the encoded length."""
    maintype = draw(st.integers(0, 7))
    subtype = draw(st.integers(0, 7))
    version = draw(st.sampled_from(versions))
    length = draw(st.integers(0, 7))
    header = ic.encode_header(maintype, subtype, version, length)
    try:
        size = ic.decode_length(maintype, length) // 8
    except ValueError:
        size = draw(st.integers(0, 40))
    return header + draw(st.binary(min_size=size, max_size=size))


def sequences(versions, min_size=0):
    # type: (tuple[int, ...], int) -> st.SearchStrategy
    """ISCC-SEQ candidates: known units, generated units and stray bytes, concatenated (at least `min_size` parts)."""
    known = st.sampled_from([ic.encode_seq([iscc]) for iscc in KNOWN_UNITS])
    parts = known | unit_bytes(versions) | st.binary(max_size=4)
    return st.lists(parts, min_size=min_size, max_size=6).map(b"".join)


def soft_bindings():
    # type: () -> st.SearchStrategy
    """Lists of soft-binding blocks as `manifest.read` returns them: any algorithm, ISCC-SEQ-like or overlong values,
    whole-asset or scoped. Long enough to exceed the io.iscc.v0 block limit of the store.
    """
    values = sequences(versions=(0,), min_size=1).filter(bool) | st.binary(min_size=1, max_size=8) | st.just(OVERLONG)
    block = st.builds(manifest.SoftBinding, alg=st.sampled_from(ALGS), value=values, scope=st.sampled_from(SCOPES))
    return st.lists(block, max_size=store.MAX_ISCC_BINDINGS * 2)


def unit_keys(units):
    # type: (list[binding.Unit]) -> list[tuple[str, bytes]]
    """The type and body of each unit: what makes two units the same."""
    return [(unit.unit_type, unit.body) for unit in units]


def check_index(blocks):
    # type: (list[manifest.SoftBinding]) -> None
    """Plan the index of soft-binding blocks: either a bounded, consistent Index or a tooManyBindings refusal."""
    try:
        index = store.plan_index(tuple(blocks))
    except manifest.InvalidManifestError as e:
        assert e.code == "manifest.tooManyBindings"
        listed = [b for b in blocks if b.alg in store.algorithms() and len(b.value) <= binding.MAX_VALUE_BYTES]
        iscc = [b for b in listed if b.alg == binding.ISCC_ALG]
        units = {key for b in iscc for key in unit_keys(store.iscc_block_units(b))}
        assert len(iscc) > store.MAX_ISCC_BINDINGS or len(units) > store.MAX_UNITS
        return
    assert all(block in blocks for block in index.bindings)
    assert all(b.alg in store.algorithms() and len(b.value) <= binding.MAX_VALUE_BYTES for b in index.bindings)
    iscc = [b for b in index.bindings if b.alg == binding.ISCC_ALG]
    assert len(index.bindings) <= store.MAX_BINDINGS
    assert len(iscc) <= store.MAX_ISCC_BINDINGS
    decoded = unit_keys([unit for b in iscc for unit in binding.decode_units(b.value)])  # kept values all decode
    whole = unit_keys([unit for b in iscc if not b.scope for unit in binding.decode_units(b.value)])
    keys = unit_keys(index.units)
    assert len(keys) == len(set(keys)) <= store.MAX_UNITS
    assert set(keys) == set(decoded)
    assert unit_keys(index.declarable) == list(dict.fromkeys(whole))


@functools.cache
def signed_store():
    # type: () -> bytes
    """One real Manifest Store with an ISCC soft binding, signed once for the mutation tests."""
    return iscc_manifest()


def mutate(data, position, value):
    # type: (bytes, int, int) -> bytes
    """The bytes with the byte at `position` (modulo their length) replaced by `value`."""
    index = position % len(data)
    return data[:index] + bytes([value]) + data[index + 1 :]


def check_read(data):
    # type: (bytes) -> None
    """Read bytes as a Manifest Store: either a summary or an InvalidManifestError, nothing else."""
    try:
        info = manifest.read(data)
    except manifest.InvalidManifestError as e:
        assert e.code
        return
    assert isinstance(info, manifest.ManifestInfo)


@PROPERTY
@given(st.text(max_size=binding.MAX_VALUE_LENGTH + 2))
def test_decode_base64_raises_only_value_error(value):
    try:
        decoded = binding.decode_base64(value)
    except ValueError:
        return
    assert isinstance(decoded, bytes)


@PROPERTY
@given(st.text(alphabet=BASE64_ALPHABET, max_size=200))
def test_decode_base64_of_base64_like_text_raises_only_value_error(value):
    try:
        decoded = binding.decode_base64(value)
    except ValueError:
        return
    assert isinstance(decoded, bytes)


@PROPERTY
@given(st.binary(max_size=binding.MAX_VALUE_BYTES))
def test_decode_base64_roundtrips_every_encoding(data):
    standard = base64.b64encode(data).decode()
    assert binding.decode_base64(standard) == data
    assert binding.decode_base64(base64.urlsafe_b64encode(data).decode().rstrip("=")) == data
    assert binding.decode_base64(standard.replace("+", " ")) == data  # also a "+" at either end
    assert binding.decode_base64(f"\t{standard}\r\n") == data


def check_units(seq):
    # type: (bytes) -> None
    """Decode an ISCC-SEQ: either distinct searchable 256-bit units or a ValueError."""
    try:
        units = binding.decode_units(seq)
    except ValueError:
        return
    assert units
    assert len({unit.iscc for unit in units}) == len(units)
    assert all(unit.maintype in binding.SEARCHABLE_TYPES and len(unit.body) == 32 for unit in units)


@PROPERTY
@given(st.binary(max_size=200))
def test_decode_units_of_random_bytes_raises_only_value_error(seq):
    check_units(seq)


@PROPERTY
@given(sequences(versions=(0,)))
def test_decode_units_of_version_0_sequences_raises_only_value_error(seq):
    check_units(seq)


@PROPERTY
@given(sequences(versions=(0, 1)))
def test_decode_units_of_any_sequence_raises_only_value_error(seq):
    check_units(seq)


@PROPERTY
@given(json_values(st.text(alphabet=st.characters(max_codepoint=127))))
def test_block_value_of_ascii_json_never_raises(value):
    assert isinstance(manifest.block_value(value), bytes)


@PROPERTY
@given(json_values(st.text()))
def test_block_value_of_any_json_never_raises(value):
    assert isinstance(manifest.block_value(value), bytes)


@PROPERTY
@given(json_values(st.text(alphabet=st.characters(max_codepoint=127))))
def test_soft_bindings_of_ascii_json_never_raise(blocks):
    data = {"alg": "io.iscc.v0", "blocks": [{"scope": {}, "value": blocks}]}
    found = manifest.soft_bindings([{"label": "c2pa.soft-binding", "data": data}])
    assert all(b.value and b.alg == "io.iscc.v0" for b in found)


@PROPERTY
@given(json_values(st.text()))
def test_soft_bindings_of_any_json_never_raise(blocks):
    data = {"alg": "io.iscc.v0", "blocks": [{"scope": {}, "value": blocks}]}
    found = manifest.soft_bindings([{"label": "c2pa.soft-binding", "data": data}])
    assert all(b.value and b.alg == "io.iscc.v0" for b in found)


@PROPERTY
@given(soft_bindings())
@example(MANY_UNITS)
@example(MANY_UNITS[:5])
@example([MANY_UNITS[0]] * (store.MAX_ISCC_BINDINGS + 1))
def test_plan_index_is_bounded_or_refuses(blocks):
    check_index(blocks)


@PROPERTY
@given(st.binary(max_size=512))
def test_read_of_random_bytes_raises_only_invalid_manifest_error(data):
    check_read(data)


@PROPERTY
@given(st.integers(min_value=0), st.integers(0, 255))
def test_read_of_a_mutated_store_raises_only_invalid_manifest_error(position, value):
    check_read(mutate(signed_store(), position, value))


@PROPERTY
@given(st.integers(min_value=0))
def test_read_of_a_truncated_store_raises_only_invalid_manifest_error(length):
    data = signed_store()
    check_read(data[: length % len(data)])
