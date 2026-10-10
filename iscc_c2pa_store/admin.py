"""Operator dashboard on Django's admin with the unfold theme: store KPIs, manifests, bindings and declarations.

Records are read-only, because they come from uploads only. The delete action deletes like
`DELETE /v1/manifests/{id}` and withdraws declarations from the hub; the retry action sends the declarations that
need attention (pending, failed, or still declared for a deleted manifest) to the hub again.
"""

from collections import Counter
from urllib.parse import urlencode

from django.conf import settings
from django.contrib import admin, messages
from django.contrib.admin import helpers
from django.contrib.admin.utils import flatten_fieldsets
from django.db.models import Model, Q, QuerySet  # noqa: F401 (type comments)
from django.http import HttpRequest  # noqa: F401 (type comments)
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils.html import format_html
from django.utils.text import Truncator
from unfold.admin import ModelAdmin, TabularInline
from unfold.decorators import action, display
from unfold.enums import ActionVariant

from iscc_c2pa_store import store
from iscc_c2pa_store.models import Binding, Declaration, IsccUnit, Manifest

Status = Declaration.Status
LATEST = 10  # rows in the dashboard lists
ALERTS = {Status.PENDING: "text-orange-700", Status.FAILED: "text-red-600"}  # highlighted declaration states
STATUS_LABELS = {
    Status.DECLARED: "success",
    Status.PENDING: "warning",
    Status.FAILED: "danger",
    Status.WITHDRAWN: "info",
    Status.SKIPPED: "default",
    Status.DISABLED: "default",
}
STATE_LABELS = {"live": "success", "deleted": "danger"}
# Declarations an operator should look at: not yet on the hub, refused by it, or still there for a deleted manifest.
NEEDS_ATTENTION = Q(status__in=tuple(ALERTS), manifest__deleted_at=None) | Q(
    status=Status.DECLARED, manifest__deleted_at__isnull=False
)

admin.site.index_template = "admin/iscc_c2pa_store/dashboard.html"


def changelist_url(model, **query):
    # type: (type[Model], str) -> str
    """The admin changelist of a model, filtered by query parameters."""
    url = reverse(f"admin:{model._meta.app_label}_{model._meta.model_name}_changelist")
    return f"{url}?{urlencode(query)}" if query else url


def percent(part, whole):
    # type: (int, int) -> int
    """`part` as a rounded percentage of `whole`, 0 for an empty whole."""
    return round(100 * part / whole) if whole else 0


def manifest_cards(stats):
    # type: (dict) -> list[dict]
    """KPI cards for manifests: live, trusted and deleted, each linking to its filtered changelist."""
    live = stats["manifests"]
    return [
        {
            "title": "Manifests",
            "value": live,
            "description": "Live Manifest Stores served by the API",
            "href": changelist_url(Manifest, state="live"),
            "icon": "inventory_2",
        },
        {
            "title": "Trusted",
            "value": stats["trusted"],
            "description": f"{percent(stats['trusted'], live)}% of live manifests signed by a trusted credential",
            "href": changelist_url(Manifest, state="live", trusted__exact="1"),
            "icon": "verified",
        },
        {
            "title": "Deleted",
            "value": stats["deleted"],
            "description": "Tombstones that answer 410 Gone",
            "href": changelist_url(Manifest, state="deleted"),
            "icon": "delete",
        },
    ]


def declaration_cards(counts):
    # type: (dict[str, int]) -> list[dict]
    """One card per declaration status of live manifests (withdrawn ones belong to deleted manifests); pending and
    failed ones get a highlight `tone` when there are any.
    """
    return [
        {
            "status": status.value,
            "count": counts.get(status, 0),
            "variant": STATUS_LABELS[status],
            "tone": ALERTS.get(status, "") if counts.get(status) else "",
            "href": changelist_url(Declaration, status__exact=status.value),
        }
        for status in Status
        if status != Status.WITHDRAWN
    ]


def binding_rows(counts):
    # type: (dict[str, int]) -> list[dict]
    """Bindings per algorithm with their C2PA list type, share of all bindings and filtered manifest changelist."""
    total = sum(counts.values())
    kinds = store.algorithms()
    return [
        {
            "alg": alg,
            "kind": kinds.get(alg, ""),
            "count": count,
            "share": percent(count, total),
            "href": changelist_url(Manifest, alg=alg),
        }
        for alg, count in counts.items()
    ]


def dashboard(request, context):
    # type: (HttpRequest, dict) -> dict
    """Add the store's KPIs, its latest uploads and the declarations that need attention to the admin index."""
    stats = store.statistics()
    attention = Declaration.objects.filter(NEEDS_ATTENTION)
    return context | {
        "store_stats": stats,
        "manifest_cards": manifest_cards(stats),
        "declaration_cards": declaration_cards(stats["declarations"]),
        "binding_rows": binding_rows(stats["bindings"]),
        "status_labels": STATUS_LABELS,
        "latest_manifests": Manifest.objects.select_related("declaration")[:LATEST],
        "attention": attention.select_related("manifest").order_by("-updated_at")[:LATEST],
        "attention_count": attention.count(),
        "attention_url": changelist_url(Declaration, attention="yes"),
        "manifests_url": changelist_url(Manifest),
        "declares": store.keypair() is not None,
        "hub_url": settings.ISCC_C2PA_STORE_HUB_URL,
        "controller": settings.ISCC_C2PA_STORE_CONTROLLER,
    }


def attention_badge(request):
    # type: (HttpRequest) -> str
    """Sidebar badge: the number of declarations that need attention, empty when there are none."""
    count = Declaration.objects.filter(NEEDS_ATTENTION).count()
    return str(count) if count else ""


def remove(manifest_id):
    # type: (str) -> bool
    """Delete a manifest and withdraw its declaration; False if it was deleted meanwhile."""
    try:
        store.delete(manifest_id)
    except LookupError:
        return False
    return True


def retry_one(declaration):
    # type: (Declaration) -> Declaration
    """Withdraw a declaration of a deleted manifest, or send a pending or failed one to the hub again."""
    if declaration.manifest.deleted_at:
        return store.withdraw(declaration)
    return store.attempt(store.requeue(declaration))


class StateFilter(admin.SimpleListFilter):
    """Live or deleted manifests."""

    title = "state"
    parameter_name = "state"

    def lookups(self, request, model_admin):
        """The two states of a manifest."""
        return [("live", "Live"), ("deleted", "Deleted")]

    def queryset(self, request, queryset):
        """Manifests in the chosen state."""
        if self.value() == "live":
            return queryset.filter(deleted_at=None)
        if self.value() == "deleted":
            return queryset.exclude(deleted_at=None)
        return queryset


class AlgorithmFilter(admin.SimpleListFilter):
    """Manifests with a soft binding of one algorithm."""

    title = "soft binding"
    parameter_name = "alg"

    def lookups(self, request, model_admin):
        """The algorithms of the stored bindings."""
        algs = Binding.objects.order_by("alg").values_list("alg", flat=True).distinct()
        return [(alg, alg) for alg in algs]

    def queryset(self, request, queryset):
        """Manifests with a binding of the chosen algorithm."""
        if self.value():
            return queryset.filter(bindings__alg=self.value()).distinct()
        return queryset


class AttentionFilter(admin.SimpleListFilter):
    """Declarations that need an operator: pending, failed, or still declared for a deleted manifest."""

    title = "attention"
    parameter_name = "attention"

    def lookups(self, request, model_admin):
        """The single choice of this filter."""
        return [("yes", "Needs attention")]

    def queryset(self, request, queryset):
        """Declarations that need attention."""
        if self.value() == "yes":
            return queryset.filter(NEEDS_ATTENTION)
        return queryset


class StoreAdmin(ModelAdmin):
    """Read-only records: uploads create them, actions change them."""

    def has_add_permission(self, request):
        """Records come from uploads only."""
        return False

    def has_change_permission(self, request, obj=None):
        """Records are shown, not edited."""
        return False

    def has_delete_permission(self, request, obj=None):
        """Deleting goes through the delete action, which withdraws declarations."""
        return False

    # Django's own: unfold 0.108 overrides it without the action_location parameter that Django 6.1 requires.
    get_action_choices = admin.ModelAdmin.get_action_choices


class ReadOnlyInline(TabularInline):
    """An inline that shows rows without editing, adding or deleting them."""

    extra = 0
    can_delete = False
    show_change_link = False

    def has_add_permission(self, request, obj=None):
        """Rows come from uploads only."""
        return False

    def has_change_permission(self, request, obj=None):
        """Rows come from uploads only."""
        return False


class BindingInline(ReadOnlyInline):
    """Soft bindings of a manifest."""

    model = Binding
    fields = ("alg", "value_hex", "scope")
    readonly_fields = fields

    @display(description="Value")
    def value_hex(self, obj):
        # type: (Binding) -> str
        """The raw binding value as hex."""
        return bytes(obj.value).hex()


class UnitInline(ReadOnlyInline):
    """Searchable ISCC units of a manifest."""

    model = IsccUnit
    fields = ("unit_type", "body_hex")
    readonly_fields = fields

    @display(description="Body")
    def body_hex(self, obj):
        # type: (IsccUnit) -> str
        """The 256-bit unit body as hex."""
        return bytes(obj.body).hex()


class DeclarationInline(ReadOnlyInline):
    """The declaration of a manifest, linking to its detail page."""

    model = Declaration
    fields = ("status", "iscc_id", "hub", "attempts", "last_error", "declared_at")
    readonly_fields = fields
    show_change_link = True


@admin.register(Manifest)
class ManifestAdmin(StoreAdmin):
    """Stored manifests: read-only records with a delete action that withdraws declarations."""

    list_display = (
        "manifest_id",
        "title",
        "signer",
        "trusted",
        "state",
        "declaration_status",
        "size",
        "uploaded_at",
    )
    list_filter = (StateFilter, "trusted", "declaration__status", AlgorithmFilter, "validation_state")
    list_select_related = ("declaration",)
    search_fields = ("manifest_id", "title", "signer", "claim_generator", "sha256", "declaration__iscc_id")
    date_hierarchy = "uploaded_at"
    fieldsets = (
        (None, {"fields": ("manifest_id", "address", "title", "media_type", "state")}),
        (
            "Signature",
            {"fields": ("signer", "signed_at", "claim_generator", "trusted", "validation_state", "failures")},
        ),
        ("Upload", {"fields": ("sha256", "size", "uploader", "uploaded_at", "deleted_at")}),
    )
    readonly_fields = flatten_fieldsets(fieldsets)
    inlines = (DeclarationInline, BindingInline, UnitInline)
    actions = ("delete_manifests", "release_ids")

    @display(description="State", label=STATE_LABELS, ordering="deleted_at")
    def state(self, obj):
        # type: (Manifest) -> str
        """Whether the manifest is served or deleted."""
        return "deleted" if obj.deleted_at else "live"

    @display(description="Declaration", label=STATUS_LABELS, ordering="declaration__status")
    def declaration_status(self, obj):
        # type: (Manifest) -> str
        """Where the declaration of this manifest stands."""
        return obj.declaration.status

    @display(description="Address")
    def address(self, obj):
        # type: (Manifest) -> str
        """The public address of the manifest, which is also its ISCC gateway URL."""
        url = store.manifest_url(obj.manifest_id)
        return format_html('<a href="{}" class="text-primary-600" rel="noopener">{}</a>', url, url)

    def has_delete_manifests_permission(self, request):
        # type: (HttpRequest) -> bool
        """The delete action needs Django's delete permission on manifests."""
        return request.user.has_perm("iscc_c2pa_store.delete_manifest")

    @action(
        description="Delete manifests and withdraw their declarations",
        permissions=("delete_manifests",),
        icon="delete",
        variant=ActionVariant.DANGER,
    )
    def delete_manifests(self, request, queryset):
        # type: (HttpRequest, QuerySet[Manifest]) -> TemplateResponse | None
        """Ask for confirmation, then delete the live selected manifests like `DELETE /v1/manifests/{id}` does."""
        live = queryset.filter(deleted_at=None).select_related("declaration")
        if not live.exists():
            self.message_user(request, "No live manifests selected", messages.WARNING)
            return None
        if request.POST.get("post") != "yes":
            return self.confirm_delete(request, live)
        ids = [record.manifest_id for record in live]
        deleted = sum(remove(manifest_id) for manifest_id in ids)
        self.message_user(request, f"Deleted {deleted} manifest(s)", messages.SUCCESS)
        due = Declaration.objects.filter(manifest_id__in=ids, status=Status.DECLARED).count()
        if due:
            self.message_user(request, f"{due} withdrawal(s) failed; the retry loop tries again", messages.WARNING)
        return None

    @action(
        description="Release the IDs of deleted manifests for new uploads",
        permissions=("delete_manifests",),
        icon="lock_open",
    )
    def release_ids(self, request, queryset):
        # type: (HttpRequest, QuerySet[Manifest]) -> None
        """Forget the selected deleted manifests whose declarations are off the hub, so their IDs can be reused."""
        released = sum(store.release(record.manifest_id) for record in queryset)
        self.message_user(request, f"Released {released} manifest ID(s)", messages.SUCCESS)

    def confirm_delete(self, request, queryset):
        # type: (HttpRequest, QuerySet[Manifest]) -> TemplateResponse
        """The confirmation page of the delete action."""
        context = {
            **self.admin_site.each_context(request),
            "title": "Delete manifests",
            "opts": self.opts,
            "queryset": queryset,
            "status_labels": STATUS_LABELS,
            "action_checkbox_name": helpers.ACTION_CHECKBOX_NAME,
            "media": self.media,
        }
        return TemplateResponse(request, "admin/iscc_c2pa_store/delete_manifests.html", context)


@admin.register(Declaration)
class DeclarationAdmin(StoreAdmin):
    """Declarations: their status, hub answers and a retry action."""

    list_display = ("manifest", "status_label", "iscc_id", "attempts", "error", "declared_at", "updated_at")
    list_filter = (AttentionFilter, "status", "hub")
    list_select_related = ("manifest",)
    search_fields = ("manifest__manifest_id", "manifest__title", "iscc_id", "iscc_code")
    fieldsets = (
        (None, {"fields": ("manifest", "status", "iscc_id", "iscc_code", "units")}),
        ("Hub", {"fields": ("hub", "attempts", "last_error", "declared_at", "updated_at", "note")}),
    )
    readonly_fields = flatten_fieldsets(fieldsets)
    actions = ("retry",)

    @display(description="Status", label=STATUS_LABELS, ordering="status")
    def status_label(self, obj):
        # type: (Declaration) -> str
        """Where the declaration stands."""
        return obj.status

    @display(description="Last error")
    def error(self, obj):
        # type: (Declaration) -> str
        """The last hub error, shortened for the list."""
        return Truncator(obj.last_error).chars(80)

    def has_retry_permission(self, request):
        # type: (HttpRequest) -> bool
        """The retry action needs Django's change permission on declarations."""
        return request.user.has_perm("iscc_c2pa_store.change_declaration")

    @action(description="Retry declarations that need attention", permissions=("retry",), icon="refresh")
    def retry(self, request, queryset):
        # type: (HttpRequest, QuerySet[Declaration]) -> None
        """Send the selected pending or failed declarations again and withdraw those of deleted manifests."""
        if store.keypair() is None:
            self.message_user(request, "Declarations are disabled on this store", messages.ERROR)
            return
        outcomes = Counter(retry_one(d).status for d in queryset.filter(NEEDS_ATTENTION).select_related("manifest"))
        summary = ", ".join(f"{count} {status}" for status, count in sorted(outcomes.items()))
        message = f"Retried {outcomes.total()} declaration(s)" + (f": {summary}" if summary else "")
        self.message_user(request, message, messages.INFO)
