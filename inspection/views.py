from django.shortcuts import get_object_or_404
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import status, viewsets
from rest_framework.exceptions import PermissionDenied
from rest_framework.filters import OrderingFilter, SearchFilter
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import InspectionBatch, InspectionRecord, Product, ReviewEvent, record_review
from .permissions import (
    CanSubmitInspections,
    IsAdministrator,
    get_user_role,
    is_engineer,
    is_supervisor,
)
from .serializers import (
    InspectionBatchSerializer,
    InspectionRecordSerializer,
    MeSerializer,
    ProductSerializer,
    ReviewEventSerializer,
    SupervisorReviewSerializer,
)
from .tasks import process_single_inspection
from .utils import validate_batch_size, validate_image_file


def _forbidden(message: str) -> PermissionDenied:
    """DRF renders this as a JSON 403 with the readable reason."""
    return PermissionDenied(detail=message)


class MeView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        role = get_user_role(request.user)
        if not role:
            raise _forbidden("No RBAC profile is assigned to this account.")
        return Response(
            MeSerializer(
                {
                    "id": request.user.id,
                    "username": request.user.get_username(),
                    "role": role,
                }
            ).data
        )


class ProductViewSet(viewsets.ModelViewSet):
    queryset = Product.objects.all()
    serializer_class = ProductSerializer
    permission_classes = [IsAdministrator]
    lookup_field = "product_id"


class BatchUploadView(APIView):
    permission_classes = [IsAuthenticated, CanSubmitInspections]
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request):
        files = request.FILES.getlist("images") or request.FILES.getlist("files")
        if not files and request.FILES:
            files = list(request.FILES.values())

        size_error = validate_batch_size(len(files))
        if size_error:
            return Response({"detail": size_error}, status=status.HTTP_400_BAD_REQUEST)

        # Engineers must name the product line; no silent demo fallback.
        product_code = (
            request.data.get("product_id")
            or request.query_params.get("product_id")
            or ""
        )
        if not product_code:
            return Response(
                {"detail": "product_id is required. Register products as an "
                            "administrator, then select one when uploading."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        product = Product.objects.filter(product_id=product_code, is_active=True).first()
        if product is None:
            return Response(
                {"detail": f"Unknown or inactive product '{product_code}'."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        batch = InspectionBatch.objects.create(
            product=product,
            submitted_by=request.user,
            status=InspectionBatch.Status.QUEUED,
            notes=(request.data.get("notes") or "")[:255],
        )

        queued_ids = []
        for uploaded in files:
            filename = getattr(uploaded, "name", "unnamed") or "unnamed"
            reason = validate_image_file(uploaded)
            if reason:
                InspectionRecord.objects.create(
                    batch=batch,
                    submitted_by=request.user,
                    original_filename=filename,
                    status=InspectionRecord.Status.FAILED,
                    failure_reason=reason,
                )
                continue

            record = InspectionRecord.objects.create(
                batch=batch,
                submitted_by=request.user,
                original_filename=filename,
                image=uploaded,
                status=InspectionRecord.Status.QUEUED,
            )
            queued_ids.append(record.pk)

        if queued_ids:
            # One Celery task per valid image, as on the challenge flow.
            results = [process_single_inspection.delay(pk) for pk in queued_ids]
            batch.celery_task_id = (results[0].id or "")[:64]
            batch.save(update_fields=["celery_task_id"])
            # Aggregate from record statuses; in eager mode the tasks have
            # already finished, and this never regresses a settled batch.
            batch.refresh_status(save=True)
        else:
            batch.refresh_status(save=True)

        batch = (
            InspectionBatch.objects
            .prefetch_related("records__review_events__reviewer")
            .get(pk=batch.pk)
        )
        return Response(
            InspectionBatchSerializer(batch).data,
            status=status.HTTP_201_CREATED,
        )


class InspectionBatchViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = InspectionBatchSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, OrderingFilter]
    filterset_fields = ["status", "product__product_id"]
    ordering_fields = ["created_at", "id"]

    def get_queryset(self):
        qs = InspectionBatch.objects.select_related(
            "product", "submitted_by"
        ).prefetch_related("records__review_events__reviewer")
        user = self.request.user
        if is_supervisor(user):
            return qs
        if is_engineer(user):
            return qs.filter(submitted_by=user)
        return qs.none()


class InspectionRecordViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = InspectionRecordSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = {
        "status": ["exact"],
        "predicted_class": ["exact"],
        "reviewed_class": ["exact"],
        "batch": ["exact"],
        "batch__product__product_id": ["exact"],
        "submitted_by__username": ["exact"],
        "created_at": ["date", "gte", "lte"],
    }
    search_fields = ["original_filename", "failure_reason"]
    ordering_fields = ["created_at", "id", "confidence"]

    def get_queryset(self):
        qs = InspectionRecord.objects.select_related(
            "batch",
            "batch__product",
            "submitted_by",
            "reviewer",
        ).prefetch_related("review_events__reviewer")
        user = self.request.user
        if is_supervisor(user):
            return qs
        if is_engineer(user):
            return qs.filter(submitted_by=user)
        return qs.none()


class SupervisorReviewView(APIView):
    """
    Supervisors may confirm or correct the operational label. Engineers and
    administrators are refused even if they know the URL, and every accepted
    submission is appended to the ReviewEvent audit trail. Original model
    outputs are never written from here.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        """Review chain, readable by supervisors and by the submitting engineer."""
        record = get_object_or_404(InspectionRecord, pk=pk)
        if not is_supervisor(request.user) and record.submitted_by_id != request.user.id:
            raise _forbidden("You may not read the review history of this record.")
        events = ReviewEvent.objects.filter(record=record).select_related("reviewer")
        return Response(ReviewEventSerializer(events, many=True).data)

    def post(self, request, pk):
        return self._review(request, pk, source=ReviewEvent.Source.API)

    def patch(self, request, pk):
        return self._review(request, pk, source=ReviewEvent.Source.API)

    def _review(self, request, pk, source):
        if is_engineer(request.user):
            raise _forbidden(
                "Engineers are not permitted to review or correct predictions."
            )
        if not is_supervisor(request.user):
            raise _forbidden(
                "Only supervisors may review or correct predictions."
            )

        record = get_object_or_404(InspectionRecord, pk=pk)
        serializer = SupervisorReviewSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        if record.status != InspectionRecord.Status.COMPLETED:
            return Response(
                {
                    "detail": (
                        "Only completed inferences can be reviewed. "
                        f"Current status is '{record.status}'."
                    )
                },
                status=status.HTTP_409_CONFLICT,
            )

        record_review(
            record=record,
            reviewer=request.user,
            reviewed_class=serializer.validated_data["reviewed_class"],
            comment=serializer.validated_data.get("review_comment", ""),
            source=source,
        )
        record.refresh_from_db()
        return Response(InspectionRecordSerializer(record).data)
