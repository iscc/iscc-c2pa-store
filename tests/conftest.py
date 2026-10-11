"""Shared fixtures: real C2PA Manifest Stores signed by c2pa-rs with a throwaway certificate chain, IEP-0020 test
vectors, and a fake ISCC hub that checks signatures the way iscc-hub does.

Manifest Stores are made per test session by `sign_manifest`, so no private key is committed. The chain is a
throwaway CA and an ES256 leaf with the extensions the C2PA certificate profile requires; `trust_anchors` writes
the CA as a trust list, for tests of the trusted path.
"""

import base64
import datetime as dt
import functools
import io
import itertools
import json
import pathlib
from dataclasses import dataclass, field

import c2pa
import httpx2
import iscc_core as ic
import iscc_crypto as icr
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from django.core.cache import cache
from django.test import Client
from PIL import Image

DATA = pathlib.Path(__file__).parent / "data"
HUB_URL = "https://hub.example"
# A fixed test key for declarations (iscc-crypto multibase secret key); its did:web is the test store's host.
SECKEY = icr.key_generate().secret_key

# IEP-0020 Test Vector 1: Meta-, Content-Text-, Data- and Instance-Code, 256 bits each
META = "ISCC:AADVPDD4R6733NMPFH3D57VTQ4KVH6VHL74XIWGUT37V5ZG56NV3NSI"
CONTENT = "ISCC:EAD2RASIYU5IKLENP2OFI4CHZGRWYQCSW2WKX3Y6FJGOCXSYNYGLGBI"
DATA_CODE = "ISCC:GADQLNA7GRZESMRF2J7NZPNWGI3II2ST5YUN5SS6GVQ2ZQGJXPPYDNI"
INSTANCE = "ISCC:IAD2KIVPJIWJZP3KQCESJL6SVT5APEZUPOJWM6HVTAXCF7OT3VFA4NY"
VECTOR_1 = (
    "AAdXjHyPv721jyn2Pv6zhxVT+qdf+XRY1J7/XuTd82u2ySAHqIJIxTqFLI1+nFRwR8mjbEBStqyr7x4qTOFeWG4MswUwBwW0HzRySTIl0n7cvbYyNoRq"
    "U+4o3speNWGswMm734G1QAelIq9KLJy/aoCJJK/SrPoHkzR7k2Z49ZguIv3T3UoONw=="
)
# A real C2PA Manifest Store from the c2pa-rs test fixtures (sdk/tests/fixtures/ingredient/manifest_data.c2pa,
# Apache-2.0 or MIT): signed by the c2pa-rs test certificate, without soft bindings.
C2PA_RS_MANIFEST = (DATA / "manifest-c2pa-rs.c2pa").read_bytes()


def encode(units):
    # type: (list[str]) -> str
    """Encode ISCC-UNITs as a base64 `io.iscc.v0` value."""
    return base64.b64encode(ic.encode_seq(units)).decode("ascii")


def iscc_units(data):
    # type: (bytes) -> list[str]
    """The 256-bit Data-Code and Instance-Code of some bytes."""
    return [
        ic.gen_data_code(io.BytesIO(data), bits=256)["iscc"],
        ic.gen_instance_code(io.BytesIO(data), bits=256)["iscc"],
    ]


def flip(iscc, bits):
    # type: (str, int) -> str
    """The same ISCC-UNIT with the first `bits` bits of its body inverted: similarity 1 - bits/256."""
    maintype, subtype, version, _length, body = ic.iscc_decode(iscc)
    number = int.from_bytes(body, "big") ^ (((1 << bits) - 1) << (256 - bits))
    return "ISCC:" + ic.encode_component(maintype, subtype, version, 256, number.to_bytes(32, "big"))


def name(common_name):
    # type: (str) -> x509.Name
    """An X.509 name with an organization, as the C2PA certificate profile requires."""
    return x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, common_name),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "ISCC Test"),
        ]
    )


@dataclass(frozen=True)
class Chain:
    """A throwaway CA and ES256 signing certificate."""

    key: ec.EllipticCurvePrivateKey
    pem: str  # leaf and CA, PEM
    ca_pem: str


def make_chain(now=None):
    # type: (dt.datetime | None) -> Chain
    """Create a CA and an ES256 leaf certificate that c2pa-rs accepts for claim signing, valid for 30 days from
    `now` (by default the current time)."""
    now = now or dt.datetime.now(dt.UTC)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca = (
        x509.CertificateBuilder()
        .subject_name(name("ISCC Test Root"))
        .issuer_name(name("ISCC Test Root"))
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, False, False), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    key = ec.generate_private_key(ec.SECP256R1())
    leaf = (
        x509.CertificateBuilder()
        .subject_name(name("ISCC Test Signer"))
        .issuer_name(ca.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.KeyUsage(True, False, False, False, False, False, False, False, False), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.EMAIL_PROTECTION]), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    ca_pem = ca.public_bytes(serialization.Encoding.PEM).decode()
    return Chain(key=key, pem=leaf.public_bytes(serialization.Encoding.PEM).decode() + ca_pem, ca_pem=ca_pem)


CHAIN = make_chain()
JPEG = io.BytesIO()
Image.new("RGB", (32, 32), (0, 84, 178)).save(JPEG, "JPEG")


def es256(data, chain=CHAIN):
    # type: (bytes, Chain) -> bytes
    """Sign for c2pa-rs: ES256 with the raw r||s signature encoding of COSE."""
    r, s = decode_dss_signature(chain.key.sign(data, ec.ECDSA(hashes.SHA256())))
    return r.to_bytes(32, "big") + s.to_bytes(32, "big")


def signer(chain=CHAIN):
    # type: (Chain) -> c2pa.Signer
    """A c2pa-rs signer with the key and certificates of a chain."""
    return c2pa.Signer.from_callback(functools.partial(es256, chain=chain), c2pa.C2paSigningAlg.ES256, chain.pem, None)


def sign_manifest(bindings=None, title="test.jpg", chain=CHAIN):
    # type: (list[tuple[str, str]] | None, str, Chain) -> bytes
    """A fresh C2PA Manifest Store for a small JPEG, with soft-binding assertions given as (alg, base64 value)."""
    assertions = [
        {"label": "c2pa.actions", "data": {"actions": [{"action": "c2pa.created", "digitalSourceType": DIGITAL}]}}
    ]
    for alg, value in bindings or []:
        assertions.append(
            {"label": "c2pa.soft-binding", "data": {"alg": alg, "blocks": [{"scope": {}, "value": value}]}}
        )
    definition = {
        "claim_generator_info": [{"name": "iscc-c2pa-store-tests", "version": "1.0"}],
        "title": title,
        "format": "image/jpeg",
        "assertions": assertions,
    }
    with c2pa.Builder(definition) as builder:
        return builder.sign(signer(chain), "image/jpeg", io.BytesIO(JPEG.getvalue()), io.BytesIO())


DIGITAL = "http://cv.iptc.org/newscodes/digitalsourcetype/digitalCapture"
COUNTER = itertools.count()


def iscc_manifest(units=None):
    # type: (list[str] | None) -> bytes
    """A Manifest Store with an `io.iscc.v0` soft binding; by default with fresh Data- and Instance-Codes."""
    units = units or iscc_units(f"asset {next(COUNTER)}".encode() * 100)
    return sign_manifest([("io.iscc.v0", encode(units))])


@pytest.fixture(autouse=True)
def fresh_cache():
    """Start and end every test with an empty Django cache.

    The process-wide local-memory cache holds rate limit counters and the cached statistics, so tests would
    otherwise see counts left behind by tests in other files.
    """
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def trust_anchors(tmp_path):
    # type: (pathlib.Path) -> pathlib.Path
    """A trust list with the test CA, which makes test manifests trusted."""
    path = tmp_path / "trust.pem"
    path.write_text(CHAIN.ca_pem, encoding="utf-8")
    return path


@dataclass
class FakeHub:
    """An ISCC hub that checks note signatures and the IsccNote schema, and records every request.

    `mode` makes it answer `ok` (201), `refuse` (422), `fail` (503) or `down` (connection error).
    """

    mode: str = "ok"
    requests: list[httpx2.Request] = field(default_factory=list)
    declared: dict[str, dict] = field(default_factory=dict)

    def __call__(self, request):
        # type: (httpx2.Request) -> httpx2.Response
        """Answer one request like iscc-hub does."""
        self.requests.append(request)
        if self.mode == "down":
            raise httpx2.ConnectError("hub down", request=request)
        if self.mode == "fail":
            return httpx2.Response(503, text="Service Unavailable")
        if self.mode == "refuse":
            return httpx2.Response(422, json={"error": {"code": "invalid_iscc", "message": "refused"}})
        note = json.loads(request.content)
        if not icr.verify_json(note).signature_valid:
            return httpx2.Response(401, json={"error": {"code": "invalid_signature", "message": "bad"}})
        if request.method == "DELETE":
            return self.delete(note)
        return self.declare(request, note)

    def declare(self, request, note):
        # type: (httpx2.Request, dict) -> httpx2.Response
        """Sequence a declaration and return its ISCC-ID."""
        assert request.url.path == "/declaration"
        assert request.headers["X-Force-Declaration"] == "true"
        assert note["$schema"] == "http://purl.org/iscc/schema/iscc-note-0.8.0.json"
        iscc_id = "ISCC:" + ic.encode_component(
            ic.MT.ID, ic.ST_ID_REALM.REALM_0, ic.VS.V1, 64, len(self.declared).to_bytes(8, "big")
        )
        self.declared[iscc_id] = note
        return httpx2.Response(201, json={"iscc_id": iscc_id, "seq": len(self.declared)})

    def delete(self, note):
        # type: (dict) -> httpx2.Response
        """Delete a declaration made with the same key."""
        original = self.declared.pop(note["iscc_id"], None)
        if original is None:
            return httpx2.Response(404, json={"error": {"code": "not_found", "message": "gone"}})
        assert original["signature"]["pubkey"] == note["signature"]["pubkey"]
        return httpx2.Response(204)


@pytest.fixture
def hub(settings):
    # type: (object) -> FakeHub
    """Enable declarations against a fake hub."""
    fake = FakeHub()
    settings.ISCC_C2PA_STORE_SECKEY = SECKEY
    settings.ISCC_C2PA_STORE_HUB_URL = HUB_URL
    settings.ISCC_C2PA_STORE_HUB_TRANSPORT = httpx2.MockTransport(fake)
    return fake


@pytest.fixture
def client():
    # type: () -> Client
    """A Django test client."""
    return Client()


def upload(client, data, **query):
    # type: (Client, bytes, object) -> object
    """POST a Manifest Store to the store."""
    params = "&".join(f"{key}={str(value).lower()}" for key, value in query.items())
    return client.post(f"/v1/manifests{'?' + params if params else ''}", data=data, content_type="application/c2pa")
