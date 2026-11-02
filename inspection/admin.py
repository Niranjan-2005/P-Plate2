from django.contrib import admin

from .models import InspectionBatch, InspectionRecord, Product, ReviewEvent, UserProfile


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
    list_display = ("id", "product", "submitted_by", "status", "created_at")
    list_filter = ("status", "product")
    inlines = [InspectionRecordInline]


@admin.register(InspectionRecord)
class InspectionRecordAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "batch",
        "original_filename",
        "status",
        "predicted_class",
        "reviewed_class",
        "submitted_by",
    )
    list_filter = ("status", "predicted_class")
    readonly_fields = (
        "predicted_class",
        "confidence",
        "defect_probability",
        "model_version",
    )

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
    list_display = ("id", "record", "previous_class", "reviewed_class", "reviewer", "comment", "source", "created_at")
    list_filter = ("reviewed_class", "source")
    search_fields = ("record__original_filename", "comment")
    date_hierarchy = "created_at"

    def get_readonly_fields(self, request, obj=None):
        return [field.name for field in self.model._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        # The audit trail is append-only.
        return False
