"""Database model: stored Manifest Stores, their soft bindings, their ISCC units and their declarations."""

from typing import TYPE_CHECKING

from django.db import models

if TYPE_CHECKING:
    from django.db.models.manager import RelatedManager


class Manifest(models.Model):
    """A stored C2PA Manifest Store, addressed by the label of its active manifest.

    The bytes live in the configured file storage, named by their SHA-256. A deleted manifest keeps its row without
    bytes, so its address answers 410 Gone and the ID cannot be taken over by another upload.
    """

    manifest_id = models.CharField(primary_key=True, max_length=255)
    sha256 = models.CharField(max_length=64, unique=True)
    size = models.PositiveIntegerField()
    blob = models.FileField(upload_to="manifests/", max_length=255, blank=True)
    title = models.CharField(max_length=255, blank=True)
    media_type = models.CharField(max_length=255, blank=True)
    claim_generator = models.CharField(max_length=255, blank=True)
    signer = models.CharField(max_length=255, blank=True)
    signed_at = models.CharField(max_length=64, blank=True, help_text="Time of the claim signature, RFC 3339")
    trusted = models.BooleanField(help_text="Signing credential on the C2PA trust list")
    validation_state = models.CharField(max_length=32)
    failures = models.JSONField(default=list, help_text="Tolerated validation failure codes")
    uploader = models.CharField(max_length=64, help_text="Keyed hash of the client address")
    uploaded_at = models.DateTimeField(auto_now_add=True, db_index=True)
    deleted_at = models.DateTimeField(null=True, blank=True)

    if TYPE_CHECKING:  # reverse relations, for the type checker
        bindings: RelatedManager["Binding"]
        units: RelatedManager["IsccUnit"]
        declaration: "Declaration"

    class Meta:
        ordering = ("-uploaded_at",)

    def __str__(self):
        # type: () -> str
        return self.manifest_id


class Binding(models.Model):
    """One soft-binding block of a stored manifest, indexed by algorithm and raw value for exact matching."""

    manifest = models.ForeignKey(Manifest, on_delete=models.CASCADE, related_name="bindings")
    alg = models.CharField(max_length=255)
    value = models.BinaryField()
    scope = models.JSONField(default=dict)

    class Meta:
        indexes = (models.Index(fields=["alg", "value"]),)

    def __str__(self):
        # type: () -> str
        return self.alg


class IsccUnit(models.Model):
    """A searchable 256-bit ISCC-UNIT of a stored manifest's `io.iscc.v0` binding: the similarity table."""

    manifest = models.ForeignKey(Manifest, on_delete=models.CASCADE, related_name="units")
    unit_type = models.CharField(max_length=32, help_text="MainType, SubType and Version, e.g. CONTENT_IMAGE_V0")
    body = models.BinaryField()

    class Meta:
        constraints = (models.UniqueConstraint(fields=["manifest", "unit_type", "body"], name="unique_unit"),)
        indexes = (models.Index(fields=["unit_type", "body"]),)

    def __str__(self):
        # type: () -> str
        return self.unit_type


class Declaration(models.Model):
    """The store's ISCC declaration of a manifest on an ISCC hub, and where it stands."""

    class Status(models.TextChoices):
        DECLARED = "declared"
        PENDING = "pending"  # the hub was unreachable or failed; the retry loop tries again
        FAILED = "failed"  # the hub refused the declaration
        SKIPPED = "skipped"  # no usable io.iscc.v0 binding, or the uploader asked with declare=false
        DISABLED = "disabled"  # the deployment has no declaration key
        WITHDRAWN = "withdrawn"  # its manifest was deleted; a declaration the hub held is deleted there too

    manifest = models.OneToOneField(Manifest, on_delete=models.CASCADE, primary_key=True, related_name="declaration")
    status = models.CharField(max_length=16, choices=Status.choices, db_index=True)
    hub = models.URLField(blank=True)
    hub_id = models.PositiveSmallIntegerField(default=0, help_text="ID of the hub, for the nonces of its notes")
    iscc_code = models.CharField(max_length=128, blank=True)
    iscc_id = models.CharField(max_length=64, blank=True, db_index=True)
    units = models.JSONField(default=list, help_text="ISCC-UNITs to declare, Instance-Code last")
    note = models.JSONField(default=dict, blank=True, help_text="The last signed IsccNote sent to the hub")
    attempts = models.PositiveIntegerField(default=0)
    last_error = models.TextField(blank=True)
    declared_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    if TYPE_CHECKING:
        manifest_id: str

    def __str__(self):
        # type: () -> str
        return f"{self.manifest_id}: {self.status}"
