"""Tests of Hamming similarity over 256-bit unit bodies and the integer score rule shared with the resolver."""

from fractions import Fraction

import iscc_core as ic
import numpy as np
import pytest
from conftest import CONTENT, DATA_CODE, flip

from iscc_c2pa_store.similarity import BEST_META, BEST_SIMILAR, BITS, EQUAL, hamming_similarity, percent


def body(iscc):
    # type: (str) -> bytes
    """The 32-byte body of an ISCC-UNIT."""
    return ic.iscc_decode(iscc)[4]


def half_up(similarity):
    # type: (Fraction) -> int
    """Exact reference for `percent`: round a similarity to a percentage half up, capped at 99, at least 0."""
    scaled = similarity * 100 + Fraction(1, 2)
    return min(BEST_SIMILAR, max(0, scaled.numerator // scaled.denominator))


def test_constants():
    assert (BITS, EQUAL, BEST_SIMILAR, BEST_META) == (256, 100, 99, 85)


@pytest.mark.parametrize(("similarity", "expected"), [(1.0, 85), (0.86, 85), (0.849, 85), (0.844, 84), (0.75, 75)])
def test_percent_with_meta_ceiling(similarity, expected):
    assert percent(similarity, BEST_META) == expected


def test_hamming_similarity_of_identical_body_is_one():
    assert hamming_similarity(body(CONTENT), [body(CONTENT)]).tolist() == [1.0]


@pytest.mark.parametrize(("bits", "expected"), [(1, 0.99609375), (8, 0.96875), (64, 0.75), (128, 0.5), (256, 0.0)])
def test_hamming_similarity_of_flipped_bits(bits, expected):
    assert hamming_similarity(body(CONTENT), [body(flip(CONTENT, bits))]).tolist() == [expected]


def test_hamming_similarity_of_many_bodies_keeps_order():
    bodies = [body(flip(CONTENT, 16)), body(CONTENT), body(flip(CONTENT, 200))]
    result = hamming_similarity(body(CONTENT), bodies)
    assert result.shape == (3,)
    assert result.tolist() == [1 - 16 / 256, 1.0, 1 - 200 / 256]


def test_hamming_similarity_of_complement_is_zero():
    complement = bytes(byte ^ 0xFF for byte in body(DATA_CODE))
    assert hamming_similarity(body(DATA_CODE), [complement]).tolist() == [0.0]


def test_hamming_similarity_counts_differing_bits_anywhere():
    query = bytes(32)
    stored = bytes([0b10100000] + [0] * 30 + [0b00000001])  # three bits set in the first and last byte
    assert hamming_similarity(query, [stored]).tolist() == [1 - 3 / 256]


def test_hamming_similarity_is_symmetric():
    a, b = body(CONTENT), body(DATA_CODE)
    assert hamming_similarity(a, [b]).tolist() == hamming_similarity(b, [a]).tolist()


def test_hamming_similarity_of_no_bodies_is_empty():
    result = hamming_similarity(body(CONTENT), [])
    assert isinstance(result, np.ndarray)
    assert result.shape == (0,)


@pytest.mark.parametrize(
    ("similarity", "expected"),
    [
        (1.0, 99),  # an equal Content-Code still scores below an equal Instance-Code
        (1 - 1 / 256, 99),  # 99.609375 rounds to 100, capped
        (0.995, 99),  # 99.5 rounds to 100, capped
        (1 - 3 / 256, 99),  # 98.828125
        (1 - 4 / 256, 98),  # 98.4375
        (0.5, 50),
        (1 - 96 / 256, 63),  # 62.5 rounds half up, not to even
        (1 - 224 / 256, 13),  # 12.5 rounds half up, not to even
        (0.0, 0),
        (1.5, 99),  # out of range above
        (-0.25, 0),  # out of range below
    ],
)
def test_percent(similarity, expected):
    assert percent(similarity) == expected


def test_percent_returns_int():
    assert type(percent(0.7)) is int


def test_percent_of_every_hamming_distance():
    stored = [(2**distance - 1).to_bytes(32, "big") for distance in range(BITS + 1)]
    scores = [percent(similarity) for similarity in hamming_similarity(bytes(32), stored)]
    assert scores == [half_up(1 - Fraction(distance, BITS)) for distance in range(BITS + 1)]
