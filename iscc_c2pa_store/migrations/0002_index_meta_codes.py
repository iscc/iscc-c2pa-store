"""Index the Meta-Codes of stored `io.iscc.v0` bindings, which earlier versions of the store left out.

Existing declarations stay as they were declared. A block that held only a Meta-Code was not stored at all, so it is
not recovered here; the demo app writes every unit into one block, which this migration covers.
"""

import iscc_core as ic
from django.db import migrations

from iscc_c2pa_store import binding


def meta_units(value):
    # type: (bytes) -> list[binding.Unit]
    """The searchable Meta-Codes of a stored `io.iscc.v0` binding value."""
    return [unit for unit in binding.decode_units(value) if unit.maintype == ic.MT.META]


def index_meta_codes(apps, schema_editor):
    """Add an IsccUnit row for each Meta-Code of every stored `io.iscc.v0` binding."""
    Binding = apps.get_model("iscc_c2pa_store", "Binding")
    IsccUnit = apps.get_model("iscc_c2pa_store", "IsccUnit")
    rows = Binding.objects.filter(alg=binding.ISCC_ALG).values_list("manifest_id", "value")
    units = [
        IsccUnit(manifest_id=manifest_id, unit_type=unit.unit_type, body=unit.body)
        for manifest_id, value in rows
        for unit in meta_units(bytes(value))
    ]
    IsccUnit.objects.bulk_create(units, ignore_conflicts=True)


class Migration(migrations.Migration):
    dependencies = [("iscc_c2pa_store", "0001_initial")]

    operations = [migrations.RunPython(index_meta_codes, migrations.RunPython.noop)]
