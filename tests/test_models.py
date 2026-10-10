"""Model tests: how stored manifests, bindings, ISCC units and declarations name themselves (admin, logs), their
ordering, and the hub a declaration belongs to.
"""

import pytest
from conftest import VECTOR_1, iscc_manifest, sign_manifest

from iscc_c2pa_store import store
from iscc_c2pa_store.models import Binding, Declaration, IsccUnit, Manifest


@pytest.mark.django_db
def test_str_of_stored_records():
    record = store.ingest(sign_manifest([("io.iscc.v0", VECTOR_1)]), True, "uploader")
    assert str(Manifest.objects.get()) == record.manifest_id
    assert str(Binding.objects.get()) == "io.iscc.v0"
    assert sorted(str(unit) for unit in IsccUnit.objects.all()) == [
        "CONTENT_TEXT_V0",
        "DATA_NONE_V0",
        "INSTANCE_NONE_V0",
    ]
    assert str(Declaration.objects.get()) == f"{record.manifest_id}: disabled"


@pytest.mark.django_db
def test_declaration_hub_id(settings, hub):
    settings.ISCC_C2PA_STORE_HUB_ID = 4095
    planned = store.ingest(iscc_manifest(), True, "uploader")
    assert Declaration.objects.get(pk=planned.pk).hub_id == 4095
    unplanned = store.ingest(sign_manifest(), True, "uploader")
    assert Declaration.objects.get(pk=unplanned.pk).hub_id == 0
    assert Declaration._meta.get_field("hub_id").default == 0


@pytest.mark.django_db
def test_manifests_are_ordered_newest_first():
    first = store.ingest(sign_manifest(), True, "uploader")
    second = store.ingest(sign_manifest(), True, "uploader")
    Manifest.objects.filter(pk=first.pk).update(uploaded_at=second.uploaded_at.replace(year=2000))
    assert [m.manifest_id for m in Manifest.objects.all()] == [second.manifest_id, first.manifest_id]
