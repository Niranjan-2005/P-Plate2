import logging

from celery import shared_task
from django.db import transaction

from .ml import run_predict
from .models import InspectionBatch, InspectionRecord

logger = logging.getLogger("inspection")


@shared_task(bind=True, ignore_result=False)
def process_single_inspection(self, record_id: int) -> dict:
    """
    Process one queued batch item: queued -> processing -> completed|failed.

    One Celery task is dispatched per valid upload, so the queue tracker
    reflects each image independently. Records rejected by pre-inference
    validation are already stored as failed and are never touched here, so
    they never reach the frozen model.
    """
    try:
        record = InspectionRecord.objects.select_related("batch").get(pk=record_id)
    except InspectionRecord.DoesNotExist:
        logger.error("Record %s does not exist", record_id)
        return {"record_id": record_id, "error": "not_found"}

    if record.status != InspectionRecord.Status.QUEUED:
        # Duplicate or late dispatch: never re-run inference over a stored
        # result, but still reconcile the batch in case the prior task died
        # before aggregating.
        _refresh_batch(record.batch_id)
        return {"record_id": record_id, "status": record.status, "skipped": True}

    _process_record(record)
    _refresh_batch(record.batch_id)

    logger.info(
        "Record %s finished status=%s batch=%s",
        record_id,
        record.status,
        record.batch_id,
    )
    return {"record_id": record_id, "status": record.status}


def _refresh_batch(batch_id: int) -> None:
    """
    Recompute the batch status from its records after a single item finishes.

    Queued-only stays queued; any queued/processing item means processing;
    once nothing is pending the batch settles into completed, failed, or
    partial (mixed outcomes).
    """
    try:
        batch = InspectionBatch.objects.get(pk=batch_id)
    except InspectionBatch.DoesNotExist:
        logger.error("Batch %s does not exist while aggregating", batch_id)
        return
    batch.refresh_status(save=True)


def _process_record(record: InspectionRecord) -> None:
    record.status = InspectionRecord.Status.PROCESSING
    record.failure_reason = ""
    record.save(update_fields=["status", "failure_reason", "updated_at"])

    if not record.image:
        record.status = InspectionRecord.Status.FAILED
        record.failure_reason = "No image file is stored for this record."
        record.save(update_fields=["status", "failure_reason", "updated_at"])
        return

    try:
        image_path = record.image.path
    except Exception as exc:
        logger.exception("Could not access image for record %s", record.pk)
        record.status = InspectionRecord.Status.FAILED
        record.failure_reason = f"Could not access image for inference: {exc}"
        record.save(update_fields=["status", "failure_reason", "updated_at"])
        return

    try:
        result = run_predict(image_path)
        with transaction.atomic():
            locked = InspectionRecord.objects.select_for_update().get(pk=record.pk)
            if locked.predicted_class:
                locked.status = InspectionRecord.Status.COMPLETED
                locked.save(update_fields=["status", "updated_at"])
                record.status = locked.status
                return
            locked.predicted_class = result["predicted_class"]
            locked.confidence = result["confidence"]
            locked.defect_probability = result["defect_probability"]
            locked.model_version = result["model_version"]
            locked.status = InspectionRecord.Status.COMPLETED
            locked.failure_reason = ""
            locked.save()
            record.status = locked.status
            record.predicted_class = locked.predicted_class
    except Exception as exc:
        logger.exception("Inference failed for record %s", record.pk)
        record.status = InspectionRecord.Status.FAILED
        record.failure_reason = f"Inference failed: {exc}"
        record.save(update_fields=["status", "failure_reason", "updated_at"])
