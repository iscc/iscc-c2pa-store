"""ISCC declarations: signed IsccNotes for the manifests the store hosts, and the client of the ISCC hub.

The store declares the content of a manifest under its own did:web key, with the manifest's address as gateway URL,
so the ISCC Discovery Protocol leads anyone who finds the content to the manifest. A note needs a 256-bit Data-Code
and Instance-Code; the Instance-Code body is the BLAKE3 digest of the asset, so the note's `datahash` comes from the
soft binding without the asset. Every declaration is sent with `X-Force-Declaration`, because others may have
declared the same content before, which the ISCC Discovery Protocol allows.
"""

from dataclasses import dataclass
from datetime import UTC, datetime  # noqa: F401 (type comments)
from urllib.parse import quote

import httpx2
import iscc_core as ic
import iscc_crypto as icr

from iscc_c2pa_store.binding import Unit  # noqa: F401 (type comments)

NOTE_SCHEMA = "http://purl.org/iscc/schema/iscc-note-0.8.0.json"
DELETE_SCHEMA = "http://purl.org/iscc/schema/iscc-note-delete-0.8.0.json"
BLAKE3_MULTIHASH = "1e20"  # multicodec blake3 (0x1e), digest length 32 (0x20)
DECLARE_ORDER = (ic.MT.SEMANTIC, ic.MT.CONTENT, ic.MT.DATA, ic.MT.INSTANCE)
RETRYABLE = frozenset({408, 425, 429})  # client error statuses that mean "try again later"


@dataclass(frozen=True)
class Outcome:
    """Result of one hub request. `final` is False when retrying may succeed (network failure, hub error)."""

    ok: bool
    final: bool
    iscc_id: str = ""
    error: str = ""


def declarable_units(units):
    # type: (list[Unit]) -> list[str]
    """Choose the units to declare: one per MainType in Semantic, Content, Data, Instance order.

    Returns an empty list when a 256-bit Data-Code or Instance-Code is missing (IEP-0020 requires both). A
    Semantic-Code whose SubType differs from the Content-Code is left out, because an ISCC-CODE cannot combine them.
    """
    first = {}  # type: dict[ic.MT, Unit]
    for unit in units:
        first.setdefault(unit.maintype, unit)
    if ic.MT.DATA not in first or ic.MT.INSTANCE not in first:
        return []
    semantic, content = first.get(ic.MT.SEMANTIC), first.get(ic.MT.CONTENT)
    if semantic and content and semantic.unit_type.split("_")[1] != content.unit_type.split("_")[1]:
        del first[ic.MT.SEMANTIC]
    return [first[maintype].iscc for maintype in DECLARE_ORDER if maintype in first]


def gateway_url(base_url, manifest_id):
    # type: (str, str) -> str
    """The manifest address, used as gateway URL: `<base>/v1/manifests/<percent-encoded manifest ID>`."""
    return f"{base_url.rstrip('/')}/v1/manifests/{quote(manifest_id, safe='')}"


def timestamp(now):
    # type: (datetime) -> str
    """RFC 3339 UTC time with exactly three millisecond digits, as the hub requires."""
    return now.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def build_note(units, gateway, keypair, hub_id, now):
    # type: (list[str], str, icr.KeyPair, int, datetime) -> dict
    """A signed IsccNote for `POST /declaration` from declarable units (Instance-Code last)."""
    *rest, instance = units
    note = {
        "$schema": NOTE_SCHEMA,
        "iscc_code": ic.gen_iscc_code([*rest, instance])["iscc"],
        "datahash": BLAKE3_MULTIHASH + ic.iscc_decode(instance)[4].hex(),
        "nonce": icr.create_nonce(hub_id),
        "timestamp": timestamp(now),
        "gateway": gateway,
        "units": rest,
    }
    return icr.sign_json(note, keypair)


def build_delete(iscc_id, keypair, hub_id, now):
    # type: (str, icr.KeyPair, int, datetime) -> dict
    """A signed IsccNoteDelete for `DELETE /declaration/{iscc_id}`, by the key that declared."""
    note = {
        "$schema": DELETE_SCHEMA,
        "iscc_id": iscc_id,
        "nonce": icr.create_nonce(hub_id),
        "timestamp": timestamp(now),
    }
    return icr.sign_json(note, keypair)


def hub_error(response):
    # type: (httpx2.Response) -> str
    """Describe a refusing hub response by status and the hub's error code and message, when it sent them."""
    try:
        error = response.json().get("error")
    except (ValueError, AttributeError):
        error = None
    error = error if isinstance(error, dict) else {}
    detail = ": ".join(str(error[key]) for key in ("code", "message") if error.get(key))
    return f"HTTP {response.status_code}" + (f" {detail}" if detail else "")


def is_final(status):
    # type: (int) -> bool
    """Tell whether a refusing HTTP status is final: client errors are, except timeouts and rate limits."""
    return status < 500 and status not in RETRYABLE


def issued_id(response):
    # type: (httpx2.Response) -> str
    """The ISCC-ID in a 201 answer of the hub, or "" if the body does not carry one."""
    try:
        iscc_id = response.json()["iscc_id"]
    except (ValueError, KeyError, TypeError):
        return ""
    return iscc_id if isinstance(iscc_id, str) else ""


def submit(client, hub_url, note):
    # type: (httpx2.Client, str, dict) -> Outcome
    """Declare a signed note on the hub. Server errors, rate limits and network failures are retryable."""
    try:
        response = client.post(f"{hub_url}/declaration", json=note, headers={"X-Force-Declaration": "true"})
    except httpx2.HTTPError as e:
        return Outcome(ok=False, final=False, error=f"{type(e).__name__}: {e}")
    if response.status_code != 201:
        return Outcome(ok=False, final=is_final(response.status_code), error=hub_error(response))
    iscc_id = issued_id(response)
    if not iscc_id:
        return Outcome(ok=False, final=False, error="HTTP 201 without an ISCC-ID")
    return Outcome(ok=True, final=True, iscc_id=iscc_id)


def withdraw(client, hub_url, note):
    # type: (httpx2.Client, str, dict) -> Outcome
    """Delete a declaration on the hub with a signed delete note. A declaration the hub no longer has counts as gone."""
    try:
        response = client.request("DELETE", f"{hub_url}/declaration/{note['iscc_id']}", json=note)
    except httpx2.HTTPError as e:
        return Outcome(ok=False, final=False, error=f"{type(e).__name__}: {e}")
    if response.status_code in (204, 404):
        return Outcome(ok=True, final=True, iscc_id=note["iscc_id"])
    return Outcome(ok=False, final=is_final(response.status_code), error=hub_error(response))
