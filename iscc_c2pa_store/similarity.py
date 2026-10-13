"""ISCC similarity over stored units, and the score rule shared with iscc-c2pa-resolver.

Each searchable query unit is compared with every stored unit of the same type: Instance-Codes by equality,
Meta-, Semantic-, Content- and Data-Codes by the Hamming distance of their 256-bit bodies. A manifest scores 100 when
an Instance-Code is equal, otherwise its best unit score: the similarity as a percentage, capped at 99 for Semantic-,
Content- and Data-Codes and at 85 for Meta-Codes, because equal metadata is weaker evidence than equal content.
Brute force over a numpy matrix is fast enough for the first releases; an ANN index can replace `hamming_similarity`
behind the same interface when a deployment outgrows it.
"""

import math

import numpy as np

BITS = 256
EQUAL = 100
BEST_SIMILAR = 99  # a similar unit never claims the certainty of an equal Instance-Code
BEST_META = 85  # a Meta-Code match never outranks a close content match


def hamming_similarity(query, bodies):
    # type: (bytes, list[bytes]) -> np.ndarray
    """Similarity in [0, 1] of a 32-byte query body to each 32-byte stored body: 1 - Hamming distance / 256."""
    if not bodies:
        return np.zeros(0)
    matrix = np.frombuffer(b"".join(bodies), dtype=np.uint8).reshape(len(bodies), BITS // 8)
    distance = np.unpackbits(matrix ^ np.frombuffer(query, dtype=np.uint8), axis=1).sum(axis=1)
    return 1.0 - distance / BITS


def percent(similarity, ceiling=BEST_SIMILAR):
    # type: (float, int) -> int
    """Turn a similarity in [0, 1] into an integer score of at most `ceiling`, rounding half up."""
    return min(ceiling, max(0, math.floor(similarity * 100 + 0.5)))
