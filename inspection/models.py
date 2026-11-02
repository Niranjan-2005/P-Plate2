from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.utils import timezone


class UserProfile(models.Model):
    class Role(models.TextChoices):
        ADMINISTRATOR = "administrator", "Administrator"
        ENGINEER = "engineer", "Engineer"
        SUPERVISOR = "supervisor", "Supervisor"

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="profile",
    )
    role = models.CharField(max_length=32, choices=Role.choices)

    class Meta:
        db_table = "user_profile"

    def __str__(self):
        return f"{self.user.get_username()} ({self.role})"


class Product(models.Model):
    product_id = models.CharField(
        max_length=32,
        unique=True,
        default="P-PLATE01",
        help_text="Stable product identifier. Default line is P-PLATE01.",
    )
    name = models.CharField(max_length=128, default="Demonstration metal plate")
    description = models.TextField(
        blank=True,
        default="Synthetic centered plate with scratches or pits",
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "product"
        ordering = ["product_id"]

    def __str__(self):
        return f"{self.product_id} — {self.name}"


class InspectionBatch(models.Model):
    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        PROCESSING = "processing", "Processing"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"
        PARTIAL = "partial", "Partial"

    product = models.ForeignKey(
        Product,
        on_delete=models.PROTECT,
        related_name="batches",
    )
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="inspection_batches",
    )
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.QUEUED,
        db_index=True,
    )
    celery_task_id = models.CharField(max_length=64, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    notes = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        db_table = "inspection_batch"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["submitted_by", "-created_at"]),
            models.Index(fields=["product", "-created_at"]),
        ]

    def __str__(self):
        return f"Batch {self.pk} ({self.status})"

    def refresh_status(self, save=True):
        statuses = list(self.records.values_list("status", flat=True))
        if not statuses:
            self.status = self.Status.FAILED
        elif all(s == InspectionRecord.Status.QUEUED for s in statuses):
            self.status = self.Status.QUEUED
        elif any(s == InspectionRecord.Status.PROCESSING for s in statuses) or any(
            s == InspectionRecord.Status.QUEUED for s in statuses
        ):
            self.status = self.Status.PROCESSING
        elif all(s == InspectionRecord.Status.FAILED for s in statuses):
            self.status = self.Status.FAILED
        elif all(s == InspectionRecord.Status.COMPLETED for s in statuses):
            self.status = self.Status.COMPLETED
        else:
            self.status = self.Status.PARTIAL

        if self.status in (self.Status.COMPLETED, self.Status.FAILED, self.Status.PARTIAL):
            self.finished_at = self.finished_at or timezone.now()
        if save:
            self.save(update_fields=["status", "finished_at", "updated_at"])
        return self.status


class InspectionRecord(models.Model):
    """
    One uploaded image inside a batch.

    Model outputs (predicted_class, confidence, defect_probability,
    model_version) are write-once after inference. Supervisor review
    fields are stored separately and must never overwrite the original
    prediction.
    """

    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        PROCESSING = "processing", "Processing"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"

    class Label(models.TextChoices):
        NORMAL = "Normal", "Normal"
        DEFECTIVE = "Defective", "Defective"

    IMMUTABLE_MODEL_FIELDS = (
        "predicted_class",
        "confidence",
        "defect_probability",
        "model_version",
    )

    batch = models.ForeignKey(
        InspectionBatch,
        on_delete=models.CASCADE,
        related_name="records",
    )
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="inspection_records",
    )
    original_filename = models.CharField(max_length=255)
    image = models.FileField(upload_to="inspections/%Y/%m/%d/", blank=True)

    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.QUEUED,
        db_index=True,
    )
    failure_reason = models.TextField(blank=True, default="")

    predicted_class = models.CharField(
        max_length=32,
        choices=Label.choices,
        blank=True,
        default="",
    )
    confidence = models.FloatField(null=True, blank=True)
    defect_probability = models.FloatField(null=True, blank=True)
    model_version = models.CharField(max_length=64, blank=True, default="")

    reviewed_class = models.CharField(
        max_length=32,
        choices=Label.choices,
        blank=True,
        default="",
    )
    reviewer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="inspection_reviews",
    )
    review_timestamp = models.DateTimeField(null=True, blank=True)
    review_comment = models.TextField(blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "inspection_record"
        ordering = ["id"]
        indexes = [
            models.Index(fields=["status"]),
            models.Index(fields=["predicted_class"]),
            models.Index(fields=["submitted_by", "-created_at"]),
        ]

    def __str__(self):
        return f"Record {self.pk} {self.original_filename} ({self.status})"

    def clean(self):
        if self.reviewed_class and self.reviewed_class not in self.Label.values:
            raise ValidationError({"reviewed_class": "Must be Normal or Defective."})

    def save(self, *args, **kwargs):
        if self.pk:
            previous = (
                type(self)
                .objects.filter(pk=self.pk)
                .values(*self.IMMUTABLE_MODEL_FIELDS)
                .first()
            )
            if previous:
                for field in self.IMMUTABLE_MODEL_FIELDS:
                    original = previous[field]
                    if original not in (None, "") and getattr(self, field) != original:
                        setattr(self, field, original)
        super().save(*args, **kwargs)

    @property
    def effective_class(self):
        return self.reviewed_class or self.predicted_class

    @property
    def is_reviewed(self):
        return bool(self.reviewed_class and self.reviewer_id)


class ReviewEvent(models.Model):
    """
    Append-only audit trail: one row per submitted supervisor review,
    including amendments of an earlier review.

    InspectionRecord.reviewed_class/reviewer/review_timestamp/review_comment
    always mirror the newest event so existing screens and API consumers keep
    working, while this table preserves every intermediate decision. Rows are
    never updated or deleted, and the protected foreign key stops a reviewed
    record from being purged silently.
    """

    class Source(models.TextChoices):
        WEB = "web", "Web UI"
        API = "api", "API"

    record = models.ForeignKey(
        InspectionRecord,
        on_delete=models.PROTECT,
        related_name="review_events",
    )
    reviewer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="review_events",
    )
    previous_class = models.CharField(
        max_length=32,
        choices=InspectionRecord.Label.choices,
        blank=True,
        default="",
    )
    reviewed_class = models.CharField(max_length=32, choices=InspectionRecord.Label.choices)
    comment = models.TextField(blank=True, default="")
    source = models.CharField(max_length=8, choices=Source.choices, default=Source.WEB)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "review_event"
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["record", "-created_at"]),
            models.Index(fields=["-created_at"]),
        ]

    def __str__(self):
        return f"Review {self.pk}: record {self.record_id} -> {self.reviewed_class}"

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError(
                "ReviewEvent rows are append-only; submit a new review instead "
                "of modifying a recorded one."
            )
        super().save(*args, **kwargs)


def record_review(record, reviewer, reviewed_class, comment="", source=ReviewEvent.Source.WEB):
    """
    Store a supervisor decision as an audit event and mirror it on the record.

    Model outputs are untouched by design: InspectionRecord.save() re-applies
    them, so the original prediction stays immutable.
    """
    previous_class = record.reviewed_class
    with transaction.atomic():
        ReviewEvent.objects.create(
            record=record,
            reviewer=reviewer,
            previous_class=previous_class,
            reviewed_class=reviewed_class,
            comment=comment or "",
            source=source,
        )
        record.reviewed_class = reviewed_class
        record.review_comment = comment or ""
        record.reviewer = reviewer
        record.review_timestamp = timezone.now()
        record.save(
            update_fields=[
                "reviewed_class",
                "review_comment",
                "reviewer",
                "review_timestamp",
                "updated_at",
            ]
        )
    return record
