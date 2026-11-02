from rest_framework import serializers

from .models import (
    InspectionBatch,
    InspectionRecord,
    Product,
    ReviewEvent,
    UserProfile,
)


class UserPublicSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    username = serializers.CharField()


class ProductSerializer(serializers.ModelSerializer):
    class Meta:
        model = Product
        fields = (
            "id",
            "product_id",
            "name",
            "description",
            "is_active",
            "created_at",
            "updated_at",
        )
        read_only_fields = ("created_at", "updated_at")


class ReviewEventSerializer(serializers.ModelSerializer):
    reviewer = UserPublicSerializer(read_only=True)
    record_id = serializers.IntegerField(read_only=True)

    class Meta:
        model = ReviewEvent
        fields = (
            "id",
            "record_id",
            "reviewer",
            "previous_class",
            "reviewed_class",
            "comment",
            "source",
            "created_at",
        )
        read_only_fields = fields


class InspectionRecordSerializer(serializers.ModelSerializer):
    submitted_by = UserPublicSerializer(read_only=True)
    reviewer = UserPublicSerializer(read_only=True, allow_null=True)
    product_id = serializers.CharField(source="batch.product.product_id", read_only=True)
    batch_id = serializers.IntegerField(read_only=True)
    effective_class = serializers.CharField(read_only=True)
    is_reviewed = serializers.BooleanField(read_only=True)
    review_events = ReviewEventSerializer(many=True, read_only=True)

    class Meta:
        model = InspectionRecord
        fields = (
            "id",
            "batch_id",
            "product_id",
            "original_filename",
            "image",
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
            "review_events",
            "effective_class",
            "is_reviewed",
            "submitted_by",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields


class InspectionBatchSerializer(serializers.ModelSerializer):
    records = InspectionRecordSerializer(many=True, read_only=True)
    submitted_by = UserPublicSerializer(read_only=True)
    product_id = serializers.CharField(source="product.product_id", read_only=True)

    class Meta:
        model = InspectionBatch
        fields = (
            "id",
            "product_id",
            "status",
            "celery_task_id",
            "submitted_by",
            "created_at",
            "updated_at",
            "finished_at",
            "notes",
            "records",
        )
        read_only_fields = fields


class SupervisorReviewSerializer(serializers.Serializer):
    reviewed_class = serializers.ChoiceField(choices=InspectionRecord.Label.choices)
    review_comment = serializers.CharField(required=False, allow_blank=True, default="")


class MeSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    username = serializers.CharField()
    role = serializers.ChoiceField(choices=UserProfile.Role.choices)
