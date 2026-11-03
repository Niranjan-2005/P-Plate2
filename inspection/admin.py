from django.contrib import admin
from django.utils.html import format_html

from .models import InspectionBatch, InspectionRecord, Product, ReviewEvent, UserProfile

# Status / label -> pill tone (see .adm-badge--* in static/css/admin.css).
STATUS_TONES = {
    InspectionRecord.Status.COMPLETED: "green",
    InspectionRecord.Status.FAILED: "red",
    InspectionRecord.Status.PROCESSING: "amber",
    InspectionRecord.Status.QUEUED: "blue",
}


def _badge(text, tone):
    return format_html('<span class="adm-badge adm-badge--{}">{}</span>', tone, text)


def _class_badge(label):
    if not label:
        return _badge("\u2014", "gray")
    return _badge(label, "green" if label == InspectionRecord.Label.NORMAL else "red")


@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "role")
    list_filter = ("role",)
    search_fields = ("user__username",)


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ("product_id", "name", "is_active", "updated_at")
    search_fields = ("product_id", "name")


class InspectionRecordInline(admin.TabularInline):
    model = InspectionRecord
    extra = 0
    can_delete = False
    readonly_fields = (
        "original_filename",
        "status",
        "failure_reason",
        "predicted_class",
        "confidence",
        "defect_probability",
        "model_version",
        "reviewed_class",
        "reviewer",
        "review_timestamp",
        "review_comment",
    )

    def has_add_permission(self, request, obj=None):
        # Records are created by engineer uploads, never by hand.
        return False


@admin.register(InspectionBatch)
class InspectionBatchAdmin(admin.ModelAdmin):
    list_display = ("id", "product", "submitted_by", "status_badge", "created_at")
    list_filter = ("status", "product")
    inlines = [InspectionRecordInline]

    @admin.display(description="Status", ordering="status")
    def status_badge(self, obj):
        tone = {
            InspectionBatch.Status.COMPLETED: "green",
            InspectionBatch.Status.FAILED: "red",
            InspectionBatch.Status.PARTIAL: "amber",
            InspectionBatch.Status.PROCESSING: "amber",
            InspectionBatch.Status.QUEUED: "blue",
        }.get(obj.status, "gray")
        return _badge(obj.get_status_display(), tone)


@admin.register(InspectionRecord)
class InspectionRecordAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "batch",
        "original_filename",
        "status_badge",
        "prediction_badge",
        "review_badge",
        "submitted_by",
    )
    list_filter = ("status", "predicted_class")
    readonly_fields = (
        "predicted_class",
        "confidence",
        "defect_probability",
        "model_version",
    )

    @admin.display(description="Status", ordering="status")
    def status_badge(self, obj):
        return _badge(obj.get_status_display(), STATUS_TONES.get(obj.status, "gray"))

    @admin.display(description="Predicted", ordering="predicted_class")
    def prediction_badge(self, obj):
        return _class_badge(obj.predicted_class)

    @admin.display(description="Reviewed", ordering="reviewed_class")
    def review_badge(self, obj):
        if not obj.reviewed_class:
            return _badge("unreviewed", "gray")
        return _class_badge(obj.reviewed_class)

    def get_readonly_fields(self, request, obj=None):
        """
        Records are strictly read-only in admin. Predictions are model output,
        and supervisor decisions must travel through the review workflow so
        the reviewer, timestamp and audit event are recorded together.
        """
        return [field.name for field in self.model._meta.fields]

    def has_add_permission(self, request):
        return False


@admin.register(ReviewEvent)
class ReviewEventAdmin(admin.ModelAdmin):
    list_display = ("id", "record", "transition", "reviewer", "comment", "source", "created_at")
    list_filter = ("reviewed_class", "source")
    search_fields = ("record__original_filename", "comment")
    date_hierarchy = "created_at"

    @admin.display(description="Decision")
    def transition(self, obj):
        from_badge = _class_badge(obj.previous_class) if obj.previous_class else _badge("\u2014", "gray")
        return format_html(
            "{} \u2192 {}", from_badge, _badge(obj.reviewed_class, "green" if obj.reviewed_class == InspectionRecord.Label.NORMAL else "red")
        )

    def get_readonly_fields(self, request, obj=None):
        return [field.name for field in self.model._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        # The audit trail is append-only.
        return False
