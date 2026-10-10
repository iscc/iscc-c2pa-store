"""Tests of the `retry_declarations` management command, run as the container entrypoint runs it."""

import io
from datetime import UTC, datetime, timedelta

import pytest
from conftest import iscc_manifest
from django.core.management import call_command

from iscc_c2pa_store import store
from iscc_c2pa_store.models import Declaration, Manifest


def retry():
    # type: () -> str
    """Run one round of `retry_declarations` and return what it printed."""
    out = io.StringIO()
    call_command("retry_declarations", stdout=out)
    return out.getvalue()


def expire():
    # type: () -> None
    """Move the last change of every declaration back past the longest backoff, so the retry loop takes them."""
    Declaration.objects.update(updated_at=datetime.now(UTC) - store.MAX_BACKOFF - timedelta(seconds=1))


@pytest.mark.django_db
def test_retry_declarations_quiet_without_work(hub):
    assert retry() == ""
    store.ingest(iscc_manifest(), True, "uploader")
    expire()
    assert retry() == ""


@pytest.mark.django_db
def test_retry_declarations_leaves_recent_declarations(hub):
    hub.mode = "down"
    store.ingest(iscc_manifest(), True, "uploader")
    assert retry() == ""
    assert len(hub.requests) == 1


@pytest.mark.django_db
def test_retry_declarations_reports_tried(hub):
    hub.mode = "down"
    pending = store.ingest(iscc_manifest(), True, "uploader")
    hub.mode = "ok"
    orphaned = store.ingest(iscc_manifest(), True, "uploader")
    hub.mode = "fail"
    store.delete(orphaned.manifest_id)
    hub.mode = "ok"
    expire()
    assert retry() == "Retried 2 declaration(s)\n"
    assert Declaration.objects.get(pk=pending.pk).status == Declaration.Status.DECLARED
    assert Declaration.objects.get(pk=orphaned.pk).status == Declaration.Status.WITHDRAWN
    expire()
    assert retry() == ""


@pytest.mark.django_db
def test_retry_declarations_reports_unsuccessful_tries(hub):
    hub.mode = "down"
    record = store.ingest(iscc_manifest(), True, "uploader")
    expire()
    assert retry() == "Retried 1 declaration(s)\n"
    declaration = Declaration.objects.get(pk=record.pk)
    assert declaration.status == Declaration.Status.PENDING
    assert declaration.attempts == 2
    assert declaration.last_error == "ConnectError: hub down"


@pytest.mark.django_db
def test_retry_declarations_gives_up_quietly(hub):
    hub.mode = "down"
    record = store.ingest(iscc_manifest(), True, "uploader")
    Manifest.objects.update(uploaded_at=datetime.now(UTC) - store.GIVE_UP - timedelta(minutes=1))
    expire()
    assert retry() == ""
    declaration = Declaration.objects.get(pk=record.pk)
    assert declaration.status == Declaration.Status.FAILED
    assert declaration.last_error == "Gave up after 7 days"
    assert len(hub.requests) == 1
