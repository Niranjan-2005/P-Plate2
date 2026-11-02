from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import (
    BatchUploadView,
    InspectionBatchViewSet,
    InspectionRecordViewSet,
    MeView,
    ProductViewSet,
    SupervisorReviewView,
)

router = DefaultRouter()
router.register("products", ProductViewSet, basename="product")
router.register("batches", InspectionBatchViewSet, basename="batch")
router.register("records", InspectionRecordViewSet, basename="record")

urlpatterns = [
    path("me/", MeView.as_view(), name="me"),
    path("inspections/batches/", BatchUploadView.as_view(), name="batch-upload"),
    path(
        "inspections/records/<int:pk>/review/",
        SupervisorReviewView.as_view(),
        name="record-review",
    ),
    path("", include(router.urls)),
]
