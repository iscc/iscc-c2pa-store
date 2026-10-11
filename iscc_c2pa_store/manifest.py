"""Read and check an uploaded C2PA Manifest Store with c2pa-rs (through c2pa-python).

The store accepts any Manifest Store that c2pa-rs can read and whose active manifest has a verified claim signature.
The asset is never uploaded, so hard-binding failures are expected and tolerated; a signing credential outside the
trust list, or expired without a trusted time stamp, is recorded as untrusted, not refused. Every other validation
failure of the active manifest refuses the upload, so a tampered assertion (a soft binding among them) never enters
the store. Ingredient manifests changed after signing and bytes after the Manifest Store are refused as well, because
the store serves the uploaded bytes as they are.
"""

import base64
import functools
import io
import json
from dataclasses import dataclass
from pathlib import Path

import c2pa

MEDIA_TYPE = "application/c2pa"
VENDOR = Path(__file__).parent / "vendor"
TRUST_ANCHORS = VENDOR / "C2PA-TRUST-LIST.pem"
SOFT_BINDING = "c2pa.soft-binding"
MAX_ID_LENGTH = 255  # characters of a manifest ID (the active manifest label) the store accepts
SIGNATURE_VALIDATED = "claimSignature.validated"
UNTRUSTED = "signingCredential.untrusted"
EXPIRED = "signingCredential.expired"  # c2pa-rs reports an expired credential as untrusted too
MISMATCH = ".mismatch"  # suffix of the status codes of hashes and signatures that do not match their content
# Failures caused by the missing asset: its hard binding cannot be checked against a bare Manifest Store.
HARD_BINDING_FAILURES = frozenset(
    {
        "assertion.dataHash.mismatch",
        "assertion.boxesHash.mismatch",
        "assertion.bmffHash.mismatch",
        "assertion.collectionHash.mismatch",
    }
)
TOLERATED_FAILURES = HARD_BINDING_FAILURES | {UNTRUSTED, EXPIRED}


class InvalidManifestError(ValueError):
    """The upload is not an acceptable C2PA Manifest Store. `code` is a C2PA validation status code or our own."""

    def __init__(self, code, message):
        # type: (str, str) -> None
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass(frozen=True)
class SoftBinding:
    """One block of a soft-binding assertion: the algorithm, the raw value bytes and the block scope."""

    alg: str
    value: bytes
    scope: dict


@dataclass(frozen=True)
class ManifestInfo:
    """What the store keeps from a checked Manifest Store besides its bytes."""

    manifest_id: str
    title: str
    media_type: str
    claim_generator: str
    signer: str
    signed_at: str  # RFC 3339 time of the claim signature, or "" if the signature carries none
    trusted: bool
    validation_state: str
    failures: tuple[str, ...]  # tolerated failure codes of the active manifest, in report order
    bindings: tuple[SoftBinding, ...]


@functools.cache
def context(trust_anchors=TRUST_ANCHORS):
    # type: (Path) -> c2pa.Context
    """A c2pa-rs context that verifies signing credentials against the PEM trust anchors, one per process."""
    pem = trust_anchors.read_text(encoding="utf-8")
    settings = c2pa.Settings.from_dict(
        {
            "verify": {"verify_trust": True},
            "trust": {"trust_anchors": pem},
            # CAWG identity assertions are for the client to judge, and resolving them would make c2pa-rs fetch
            # DID documents from hosts the uploader names. No other network access either.
            "core": {"decode_identity_assertions": False, "allowed_network_hosts": []},
        }
    )
    return c2pa.Context(settings)


def read(data, trust_anchors=TRUST_ANCHORS):
    # type: (bytes, Path) -> ManifestInfo
    """Read, check and summarize a C2PA Manifest Store.

    :raises InvalidManifestError: if c2pa-rs cannot read it, bytes follow it, it has no active manifest, the claim
        signature does not verify, the active manifest has a validation failure other than a hard-binding or trust
        failure, or an ingredient manifest was changed after signing.
    """
    report = parse(data, trust_anchors)
    check_length(data)
    label = report.get("active_manifest")
    if not label:
        raise InvalidManifestError("manifest.missing", "The Manifest Store has no active manifest")
    if len(label) > MAX_ID_LENGTH or not label.isprintable():
        raise InvalidManifestError(
            "manifest.idInvalid", f"The manifest ID must be printable, at most {MAX_ID_LENGTH} characters"
        )
    results = report.get("validation_results", {})
    active = results.get("activeManifest", {})
    failures = [item["code"] for item in active.get("failure", [])]
    check_failures(failures, [item["code"] for item in active.get("success", [])])
    check_ingredients(results.get("ingredientDeltas", []))
    return summarize(label, report["manifests"][label], report.get("validation_state", ""), failures)


def parse(data, trust_anchors):
    # type: (bytes, Path) -> dict
    """Run c2pa-rs over the bytes and return its validation report as JSON.

    :raises InvalidManifestError: if c2pa-rs cannot read the bytes as a C2PA Manifest Store.
    """
    try:
        with c2pa.Reader(MEDIA_TYPE, io.BytesIO(data), context=context(trust_anchors)) as reader:
            return json.loads(reader.json())
    except c2pa.C2paError as e:
        raise InvalidManifestError("manifest.unreadable", str(e) or "Not a C2PA Manifest Store") from e


def check_length(data):
    # type: (bytes) -> None
    """Refuse bytes after the Manifest Store: the length its JUMBF superbox declares must be the length of the upload.

    c2pa-rs reads the first box and ignores anything after it, which the store would otherwise keep and serve.

    :raises InvalidManifestError: if the box length (LBox, or the 64-bit XLBox when LBox is 1) is not the length of
        the upload.
    """
    length = int.from_bytes(data[:4], "big")
    if length == 1:
        length = int.from_bytes(data[8:16], "big")
    if length != len(data):
        raise InvalidManifestError(
            "manifest.trailingData", "The upload must be the Manifest Store alone, as one JUMBF superbox"
        )


def check_failures(failures, successes):
    # type: (list[str], list[str]) -> None
    """Refuse an active manifest with an unverified claim signature or an intolerable failure.

    :raises InvalidManifestError: with the first refusing failure code.
    """
    refusing = [code for code in failures if code not in TOLERATED_FAILURES]
    if refusing:
        raise InvalidManifestError(refusing[0], "The active manifest failed validation")
    if SIGNATURE_VALIDATED not in successes:
        raise InvalidManifestError("claimSignature.missing", "The claim signature of the active manifest is unverified")


def check_ingredients(deltas):
    # type: (list[dict]) -> None
    """Refuse an ingredient manifest that was changed after the active manifest was signed: one whose hashes or
    signature no longer match. Other differences, such as an ingredient credential this store does not trust or that
    has expired since, are tolerated.

    :raises InvalidManifestError: with the first mismatch code of the c2pa-rs `ingredientDeltas`.
    """
    codes = [item["code"] for delta in deltas for item in delta.get("validationDeltas", {}).get("failure", [])]
    changed = [code for code in codes if code.endswith(MISMATCH)]
    if changed:
        raise InvalidManifestError(changed[0], "An ingredient manifest failed validation")


def summarize(label, manifest, validation_state, failures):
    # type: (str, dict, str, list[str]) -> ManifestInfo
    """Collect the fields the store keeps from the active manifest of a checked report."""
    signature = manifest.get("signature_info") or {}
    return ManifestInfo(
        manifest_id=label,
        title=manifest.get("title") or "",
        media_type=manifest.get("format") or "",
        claim_generator=claim_generator(manifest),
        signer=signature.get("common_name") or signature.get("issuer") or "",
        signed_at=signature.get("time") or "",
        trusted=UNTRUSTED not in failures,
        validation_state=validation_state,
        failures=tuple(failures),
        bindings=tuple(soft_bindings(manifest.get("assertions", []))),
    )


def claim_generator(manifest):
    # type: (dict) -> str
    """Name the claim generator: the v1 string, or else the `name/version` pairs of `claim_generator_info`."""
    if manifest.get("claim_generator"):
        return manifest["claim_generator"]
    infos = manifest.get("claim_generator_info") or []
    return " ".join(
        f"{info.get('name', '')}/{info['version']}" if "version" in info else info.get("name", "") for info in infos
    )


def soft_bindings(assertions):
    # type: (list[dict]) -> list[SoftBinding]
    """Every block of every soft-binding assertion that names an algorithm and carries a decodable value."""
    found = []
    for assertion in assertions:
        label = assertion.get("label", "")
        if label != SOFT_BINDING and not label.startswith(SOFT_BINDING + "__"):
            continue
        data = assertion.get("data") or {}
        alg = data.get("alg")
        for block in data.get("blocks") or []:
            value = block_value(block.get("value"))
            if isinstance(alg, str) and alg and value:
                found.append(SoftBinding(alg=alg, value=value, scope=block.get("scope") or {}))
    return found


def block_value(value):
    # type: (object) -> bytes
    """Raw bytes of a soft-binding block value: the specification's byte string, or base64 text as c2pa-rs writes it.

    Returns empty bytes for a value that is neither.
    """
    if isinstance(value, list) and all(isinstance(item, int) and 0 <= item < 256 for item in value):
        return bytes(value)
    if isinstance(value, str):
        try:
            return base64.b64decode(value, validate=True)
        except ValueError:  # binascii.Error, or text that is not ASCII
            return b""
    return b""
