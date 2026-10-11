"""Tests of the operator dashboard: index KPIs, list and detail pages, filters, search, permissions and actions.

Manifests are real signed Manifest Stores ingested through `store.ingest`; declarations go to the fake hub.
"""

from datetime import timedelta

import pytest
from conftest import C2PA_RS_MANIFEST, iscc_manifest, sign_manifest
from django.contrib.admin.utils import quote
from django.contrib.auth.models import Permission, User
from django.contrib.messages import get_messages
from django.test import Client  # noqa: F401 (type comments)
from django.urls import reverse

from iscc_c2pa_store import admin as store_admin
from iscc_c2pa_store import store
from iscc_c2pa_store.models import Declaration, Manifest

Status = Declaration.Status
MANIFESTS = "/admin/iscc_c2pa_store/manifest/"
DECLARATIONS = "/admin/iscc_c2pa_store/declaration/"


@pytest.fixture
def operator(client):
    # type: (Client) -> Client
    """The client, logged in as a superuser."""
    client.force_login(User.objects.create_superuser("operator", "operator@example.com", "operator-password"))
    return client


@pytest.fixture
def viewer(client):
    # type: (Client) -> Client
    """The client, logged in as staff that may only view manifests and declarations."""
    user = User.objects.create_user("viewer", "viewer@example.com", "viewer-password", is_staff=True)
    user.user_permissions.set(Permission.objects.filter(codename__in=["view_manifest", "view_declaration"]))
    client.force_login(user)
    return client


def change_url(model, pk):
    # type: (type, str) -> str
    """The admin detail page of a record, with the primary key quoted the way the admin links it."""
    return reverse(f"admin:iscc_c2pa_store_{model._meta.model_name}_change", args=[quote(pk)])


def ingest(data=None):
    # type: (bytes | None) -> Manifest
    """Store a Manifest Store, a fresh one with an `io.iscc.v0` binding by default."""
    return store.ingest(data or iscc_manifest(), True, "test-uploader")


def settle():
    # type: () -> None
    """Move every declaration's last change back past the lease, so no request can still be in flight."""
    Declaration.objects.update(updated_at=store.timestamp() - store.LEASE - timedelta(seconds=1))


def status(record):
    # type: (Manifest) -> str
    """The current declaration status of a stored manifest."""
    return Declaration.objects.get(pk=record.pk).status


def texts(response):
    # type: (object) -> list[str]
    """The messages a response left for the user."""
    return [str(message) for message in get_messages(response.wsgi_request)]


def run_action(client, url, name, records, **extra):
    # type: (Client, str, str, list, str) -> object
    """POST a changelist action for some records."""
    data = {"action": name, "_selected_action": [record.pk for record in records], **extra}
    return client.post(url, data)


def listed(client, url, **query):
    # type: (Client, str, str) -> set[str]
    """Primary keys a changelist shows for these query parameters."""
    return {record.pk for record in client.get(url, query).context["cl"].result_list}


def cards(response):
    # type: (object) -> dict[str, dict]
    """The declaration cards of the dashboard, by status."""
    return {card["status"]: card for card in response.context["declaration_cards"]}


# Dashboard


@pytest.mark.django_db
def test_dashboard_renders_counts(operator, hub, settings, trust_anchors):
    settings.ISCC_C2PA_STORE_TRUST_ANCHORS = trust_anchors
    first, second = ingest(), ingest()
    store.delete(first.manifest_id)
    response = operator.get("/admin/")
    assert response.status_code == 200
    assert "admin/iscc_c2pa_store/dashboard.html" in [t.name for t in response.templates]
    assert response.context["store_stats"] == {
        "manifests": 1,
        "trusted": 1,
        "deleted": 1,
        "bindings": {"io.iscc.v0": 1},
        "declarations": {"declared": 1},
    }
    values = [(card["title"], card["value"]) for card in response.context["manifest_cards"]]
    assert values == [("Manifests", 1), ("Trusted", 1), ("Deleted", 1)]
    assert "100% of live manifests" in response.context["manifest_cards"][1]["description"]
    assert cards(response)["declared"]["count"] == 1
    assert "withdrawn" not in cards(response)
    assert all(card["tone"] == "" for card in response.context["declaration_cards"])
    assert response.context["binding_rows"] == [
        {
            "alg": "io.iscc.v0",
            "kind": "fingerprint",
            "count": 1,
            "share": 100,
            "href": MANIFESTS + "?alg=io.iscc.v0",
        }
    ]
    assert list(response.context["latest_manifests"]) == [second, first]
    assert response.context["attention_count"] == 0
    assert response.context["declares"] is True
    text = response.text
    assert "Declaring to https://hub.example as did:web:store.example" in text
    assert change_url(Manifest, second.pk) in text
    assert "All declarations are in order" in text
    assert "withdrawal due" not in text


@pytest.mark.django_db
def test_dashboard_escapes_manifest_text(operator):
    ingest(sign_manifest(title="<script>alert(1)</script>.jpg"))
    response = operator.get("/admin/")
    assert "<script>alert(1)</script>" not in response.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;.jpg" in response.text


@pytest.mark.django_db
def test_dashboard_empty_store(operator):
    response = operator.get("/admin/")
    assert response.status_code == 200
    assert response.context["store_stats"]["manifests"] == 0
    assert response.context["binding_rows"] == []
    assert response.context["declares"] is False
    for snippet in ("No uploads yet", "No soft bindings stored yet", "All declarations are in order", "disabled"):
        assert snippet in response.text


@pytest.mark.django_db
def test_dashboard_highlights_pending_and_failed(operator, hub):
    hub.mode = "down"
    pending = ingest()
    hub.mode = "refuse"
    failed = ingest()
    assert (status(pending), status(failed)) == (Status.PENDING, Status.FAILED)
    response = operator.get("/admin/")
    assert cards(response)["pending"]["tone"] == "text-orange-700"
    assert cards(response)["failed"]["tone"] == "text-red-600"
    assert cards(response)["declared"]["tone"] == ""
    assert cards(response)["pending"]["href"] == DECLARATIONS + "?status__exact=pending"
    assert {d.manifest_id for d in response.context["attention"]} == {pending.pk, failed.pk}
    assert response.context["attention_count"] == 2
    assert "All 2 declaration(s) that need attention" in response.text
    assert change_url(Declaration, failed.pk) in response.text


@pytest.mark.django_db
def test_dashboard_lists_withdrawal_due(operator, hub):
    record = ingest()
    hub.mode = "down"
    store.delete(record.manifest_id)
    assert status(record) == Status.DECLARED
    response = operator.get("/admin/")
    assert [d.manifest_id for d in response.context["attention"]] == [record.pk]
    assert "withdrawal due" in response.text


def test_dashboard_needs_login(client):
    response = client.get("/admin/")
    assert response.status_code == 302
    assert response["Location"].startswith("/admin/login/")


def test_percent():
    assert store_admin.percent(1, 3) == 33
    assert store_admin.percent(0, 0) == 0


def test_changelist_url():
    assert store_admin.changelist_url(Manifest) == MANIFESTS
    assert store_admin.changelist_url(Declaration, attention="yes") == DECLARATIONS + "?attention=yes"
    assert store_admin.changelist_url(Manifest, alg="a b") == MANIFESTS + "?alg=a+b"


def test_binding_rows_shares_and_kinds():
    rows = store_admin.binding_rows({"io.iscc.v0": 3, "unlisted.alg": 1})
    assert [(r["alg"], r["kind"], r["count"], r["share"]) for r in rows] == [
        ("io.iscc.v0", "fingerprint", 3, 75),
        ("unlisted.alg", "", 1, 25),
    ]


def test_declaration_cards_cover_every_status_of_live_manifests():
    result = store_admin.declaration_cards({"failed": 2, "withdrawn": 5})
    assert [card["status"] for card in result] == [status.value for status in Status if status != Status.WITHDRAWN]
    by_status = {card["status"]: card for card in result}
    assert by_status["failed"] == {
        "status": "failed",
        "count": 2,
        "variant": "danger",
        "tone": "text-red-600",
        "href": DECLARATIONS + "?status__exact=failed",
    }
    assert by_status["pending"]["tone"] == ""


@pytest.mark.django_db
def test_attention_badge(hub):
    assert store_admin.attention_badge(None) == ""
    hub.mode = "down"
    ingest()
    assert store_admin.attention_badge(None) == "1"


@pytest.mark.django_db
def test_sidebar_navigation_with_badge(operator, hub):
    hub.mode = "refuse"
    ingest()
    response = operator.get(DECLARATIONS)
    assert response.status_code == 200
    store_items, access_items = (group["items"] for group in response.context["sidebar_navigation"])
    assert [item["title"] for item in store_items] == ["Dashboard", "Manifests", "Declarations"]
    assert [item["active"] for item in store_items] == [False, False, True]
    assert str(store_items[2]["badge_callback"]) == "1"
    assert all(item["has_permission"] for item in access_items)
    assert MANIFESTS in response.text


# Manifest list and detail pages


@pytest.mark.django_db
def test_manifest_changelist_renders(operator, hub):
    record = ingest()
    response = operator.get(MANIFESTS)
    assert response.status_code == 200
    assert response.context["cl"].result_count == 1
    text = response.text
    assert record.manifest_id in text
    assert "declared" in text
    assert "live" in text
    assert "delete_manifests" in text
    assert "delete_selected" not in text


@pytest.mark.django_db
def test_manifest_changelist_search(operator):
    wanted = ingest(sign_manifest(title="needle.jpg"))
    ingest(sign_manifest(title="hay.jpg"))
    response = operator.get(MANIFESTS, {"q": "needle"})
    assert [r.pk for r in response.context["cl"].result_list] == [wanted.pk]
    response = operator.get(MANIFESTS, {"q": wanted.sha256})
    assert [r.pk for r in response.context["cl"].result_list] == [wanted.pk]


@pytest.mark.django_db
def test_manifest_search_by_iscc_id(operator, hub):
    record = ingest()
    ingest()
    iscc_id = Declaration.objects.get(pk=record.pk).iscc_id
    response = operator.get(MANIFESTS, {"q": iscc_id})
    assert [r.pk for r in response.context["cl"].result_list] == [record.pk]


@pytest.mark.django_db
def test_manifest_filters(operator):
    live = ingest()
    gone = ingest()
    plain = ingest(C2PA_RS_MANIFEST)
    store.delete(gone.manifest_id)
    assert listed(operator, MANIFESTS) == {live.pk, gone.pk, plain.pk}
    assert listed(operator, MANIFESTS, state="live") == {live.pk, plain.pk}
    assert listed(operator, MANIFESTS, state="deleted") == {gone.pk}
    assert listed(operator, MANIFESTS, alg="io.iscc.v0") == {live.pk}
    assert listed(operator, MANIFESTS, trusted__exact="1") == set()
    assert listed(operator, MANIFESTS, declaration__status__exact="disabled") == {live.pk, gone.pk, plain.pk}
    response = operator.get(MANIFESTS)
    assert "io.iscc.v0" in response.text


@pytest.mark.django_db
def test_manifest_change_page_is_read_only(operator, hub):
    record = ingest()
    response = operator.get(change_url(Manifest, record.pk))
    assert response.status_code == 200
    text = response.text
    assert store.manifest_url(record.manifest_id) in text
    assert record.sha256 in text
    binding = record.bindings.get()
    unit = record.units.first()
    assert bytes(binding.value).hex() in text
    assert bytes(unit.body).hex() in text
    assert Declaration.objects.get(pk=record.pk).iscc_id in text
    assert change_url(Declaration, record.pk) in text
    assert 'name="_save"' not in text
    assert operator.post(change_url(Manifest, record.pk)).status_code == 403


@pytest.mark.django_db
def test_manifest_add_and_delete_refused(operator):
    record = ingest()
    assert operator.get(MANIFESTS + "add/").status_code == 403
    delete_url = reverse("admin:iscc_c2pa_store_manifest_delete", args=[record.pk])
    assert operator.get(delete_url).status_code == 403
    assert operator.post(delete_url, {"post": "yes"}).status_code == 403
    assert Manifest.objects.get(pk=record.pk).deleted_at is None


def test_display_methods():
    manifest_admin = store_admin.admin.site.get_model_admin(Manifest)
    record = Manifest(manifest_id="urn:c2pa:<x>", sha256="0" * 64, size=1, trusted=False)
    record.declaration = Declaration(manifest=record, status=Status.PENDING)
    assert manifest_admin.state(record) == "live"
    assert manifest_admin.declaration_status(record) == "pending"
    address = manifest_admin.address(record)
    assert "https://store.example/v1/manifests/urn:c2pa:%3Cx%3E" in address
    assert "<x>" not in address


# Declaration list and detail pages


@pytest.mark.django_db
def test_declaration_changelist_and_filters(operator, hub):
    declared = ingest()
    hub.mode = "refuse"
    failed = ingest()
    Declaration.objects.filter(pk=failed.pk).update(last_error="x" * 200)
    response = operator.get(DECLARATIONS)
    assert response.status_code == 200
    assert response.context["cl"].result_count == 2
    assert 'value="retry"' in response.text
    assert "x" * 79 + "…" in response.text
    assert "x" * 81 not in response.text
    attention = operator.get(DECLARATIONS, {"attention": "yes"}).context["cl"].result_list
    assert [d.pk for d in attention] == [failed.pk]
    by_status = operator.get(DECLARATIONS, {"status__exact": "declared"}).context["cl"].result_list
    assert [d.pk for d in by_status] == [declared.pk]


@pytest.mark.django_db
def test_declaration_search(operator, hub):
    record = ingest()
    other = ingest()
    iscc_id = Declaration.objects.get(pk=record.pk).iscc_id
    for query in (iscc_id, record.manifest_id):
        found = operator.get(DECLARATIONS, {"q": query}).context["cl"].result_list
        assert [d.pk for d in found] == [record.pk]
    assert other.pk != record.pk


@pytest.mark.django_db
def test_declaration_change_page(operator, hub):
    record = ingest()
    declaration = Declaration.objects.get(pk=record.pk)
    response = operator.get(change_url(Declaration, record.pk))
    assert response.status_code == 200
    assert declaration.iscc_id in response.text
    assert declaration.iscc_code in response.text
    assert change_url(Manifest, record.pk) in response.text
    assert 'name="_save"' not in response.text


@pytest.mark.django_db
def test_declaration_add_and_delete_refused(operator, hub):
    record = ingest()
    assert operator.get(DECLARATIONS + "add/").status_code == 403
    delete_url = reverse("admin:iscc_c2pa_store_declaration_delete", args=[record.pk])
    assert operator.get(delete_url).status_code == 403


# Delete action


@pytest.mark.django_db
def test_delete_action_asks_for_confirmation(operator, hub):
    record = ingest()
    response = run_action(operator, MANIFESTS, "delete_manifests", [record])
    assert response.status_code == 200
    assert "admin/iscc_c2pa_store/delete_manifests.html" in [t.name for t in response.templates]
    assert "Delete 1 manifest(s)?" in response.text
    assert 'name="post" value="yes"' in response.text
    assert Manifest.objects.get(pk=record.pk).deleted_at is None


@pytest.mark.django_db
def test_delete_action_deletes_and_withdraws(operator, hub):
    first, second, kept = ingest(), ingest(), ingest()
    assert len(hub.declared) == 3
    response = run_action(operator, MANIFESTS, "delete_manifests", [first, second], post="yes")
    assert response.status_code == 302
    assert texts(response) == ["Deleted 2 manifest(s)"]
    for record in (first, second):
        assert Manifest.objects.get(pk=record.pk).deleted_at is not None
        assert status(record) == Status.WITHDRAWN
    assert Manifest.objects.get(pk=kept.pk).deleted_at is None
    assert [note["iscc_code"] for note in hub.declared.values()] == [Declaration.objects.get(pk=kept.pk).iscc_code]
    assert [r.method for r in hub.requests].count("DELETE") == 2


@pytest.mark.django_db
def test_delete_action_reports_failed_withdrawals(operator, hub):
    record = ingest()
    hub.mode = "down"
    response = run_action(operator, MANIFESTS, "delete_manifests", [record], post="yes")
    assert texts(response) == ["Deleted 1 manifest(s)", "1 withdrawal(s) failed; the retry loop tries again"]
    assert Manifest.objects.get(pk=record.pk).deleted_at is not None
    assert status(record) == Status.DECLARED


@pytest.mark.django_db
def test_delete_action_skips_deleted_manifests(operator):
    record = ingest()
    store.delete(record.manifest_id)
    response = run_action(operator, MANIFESTS, "delete_manifests", [record])
    assert response.status_code == 302
    assert texts(response) == ["No live manifests selected"]


@pytest.mark.django_db
def test_remove_tolerates_deleted_manifest():
    record = ingest()
    assert store_admin.remove(record.manifest_id) is True
    assert store_admin.remove(record.manifest_id) is False


@pytest.mark.django_db
def test_delete_action_needs_delete_permission(viewer):
    record = ingest()
    response = viewer.get(MANIFESTS)
    assert response.status_code == 200
    assert "delete_manifests" not in response.text
    run_action(viewer, MANIFESTS, "delete_manifests", [record], post="yes")
    assert Manifest.objects.get(pk=record.pk).deleted_at is None


# Release action


@pytest.mark.django_db
def test_release_action_frees_ids_of_withdrawn_manifests(operator, hub):
    data = iscc_manifest()
    released = ingest(data)
    store.delete(released.manifest_id)
    assert status(released) == Status.WITHDRAWN
    settle()
    response = run_action(operator, MANIFESTS, "release_ids", [released])
    assert response.status_code == 302
    assert texts(response) == ["Released 1 manifest ID(s)"]
    assert not Manifest.objects.filter(pk=released.pk).exists()
    assert not Declaration.objects.filter(pk=released.pk).exists()
    again = ingest(data)
    assert again.manifest_id == released.manifest_id
    assert again.deleted_at is None


@pytest.mark.django_db
def test_release_action_keeps_live_and_declared_manifests(operator, hub):
    live = ingest()
    declared = ingest()
    hub.mode = "down"
    store.delete(declared.manifest_id)
    assert status(declared) == Status.DECLARED
    response = run_action(operator, MANIFESTS, "release_ids", [live, declared])
    assert texts(response) == ["Released 0 manifest ID(s)"]
    assert Manifest.objects.get(pk=live.pk).deleted_at is None
    assert Manifest.objects.get(pk=declared.pk).deleted_at is not None
    assert status(declared) == Status.DECLARED


@pytest.mark.django_db
def test_release_action_frees_ids_without_declaration_key(operator):
    record = ingest()
    store.delete(record.manifest_id)
    assert status(record) == Status.DISABLED
    settle()
    response = run_action(operator, MANIFESTS, "release_ids", [record])
    assert texts(response) == ["Released 1 manifest ID(s)"]
    assert not Manifest.objects.filter(pk=record.pk).exists()


@pytest.mark.django_db
def test_release_action_needs_delete_permission(viewer):
    record = ingest()
    store.delete(record.manifest_id)
    response = viewer.get(MANIFESTS)
    assert "release_ids" not in response.text
    run_action(viewer, MANIFESTS, "release_ids", [record])
    assert Manifest.objects.filter(pk=record.pk).exists()


@pytest.mark.django_db
def test_manifest_changelist_offers_release_action(operator):
    assert "release_ids" in operator.get(MANIFESTS).text


# Retry action


@pytest.mark.django_db
def test_retry_action_declares_pending_and_failed(operator, hub):
    hub.mode = "down"
    pending = ingest()
    hub.mode = "refuse"
    failed = ingest()
    hub.mode = "ok"
    response = run_action(operator, DECLARATIONS, "retry", [pending, failed])
    assert response.status_code == 302
    assert texts(response) == ["Retried 2 declaration(s): 2 declared"]
    assert (status(pending), status(failed)) == (Status.DECLARED, Status.DECLARED)
    assert len(hub.declared) == 2


@pytest.mark.django_db
def test_retry_action_reports_outcomes(operator, hub):
    hub.mode = "refuse"
    failed = ingest()
    hub.mode = "down"
    response = run_action(operator, DECLARATIONS, "retry", [failed])
    assert texts(response) == ["Retried 1 declaration(s): 1 pending"]
    declaration = Declaration.objects.get(pk=failed.pk)
    assert declaration.status == Status.PENDING
    assert declaration.attempts == 2


@pytest.mark.django_db
def test_retry_action_withdraws_declarations_of_deleted_manifests(operator, hub):
    record = ingest()
    hub.mode = "down"
    store.delete(record.manifest_id)
    hub.mode = "ok"
    response = run_action(operator, DECLARATIONS, "retry", [record])
    assert texts(response) == ["Retried 1 declaration(s): 1 withdrawn"]
    assert status(record) == Status.WITHDRAWN
    assert hub.declared == {}


@pytest.mark.django_db
def test_retry_action_skips_settled_declarations(operator, hub):
    declared = ingest()
    hub.mode = "refuse"
    failed = ingest()
    requests = len(hub.requests)
    store.delete(failed.manifest_id)
    assert status(failed) == Status.WITHDRAWN
    assert len(hub.requests) == requests
    response = run_action(operator, DECLARATIONS, "retry", [declared, failed])
    assert texts(response) == ["Retried 0 declaration(s)"]
    assert len(hub.requests) == requests
    assert (status(declared), status(failed)) == (Status.DECLARED, Status.WITHDRAWN)


@pytest.mark.django_db
def test_retry_action_without_declaration_key(operator, hub, settings):
    hub.mode = "refuse"
    failed = ingest()
    settings.ISCC_C2PA_STORE_SECKEY = ""
    response = run_action(operator, DECLARATIONS, "retry", [failed])
    assert texts(response) == ["Declarations are disabled on this store"]
    assert status(failed) == Status.FAILED


@pytest.mark.django_db
def test_retry_action_needs_change_permission(viewer, hub):
    hub.mode = "refuse"
    failed = ingest()
    hub.mode = "ok"
    response = viewer.get(DECLARATIONS)
    assert response.status_code == 200
    assert 'value="retry"' not in response.text
    run_action(viewer, DECLARATIONS, "retry", [failed])
    assert status(failed) == Status.FAILED


@pytest.mark.django_db
def test_login_leads_to_the_dashboard(client):
    """unfold's login form sends no `next`, so a login must still land on the dashboard, not /accounts/profile/."""
    User.objects.create_superuser("login", "login@example.com", "login-password")
    response = client.post("/admin/login/", {"username": "login", "password": "login-password"})
    assert response.status_code == 302
    assert response["Location"] == "/admin/"


@pytest.mark.django_db
def test_declaration_cards_hold_no_nested_links(operator, hub):
    """The status label inside a linked card is a span: a link inside a link breaks the card markup."""
    ingest()
    html = operator.get("/admin/").content.decode()
    cards = html.split('<a href="/admin/iscc_c2pa_store/declaration/?status__exact=')[1:]
    assert len(cards) == len(Declaration.Status) - 1  # every status but withdrawn
    for card in cards:
        assert "<a " not in card.split("</a>")[0]
