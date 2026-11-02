from unittest.mock import PropertyMock, patch

from django.contrib.auth import get_user_model
from django.db.models.fields.files import FieldFile
from django.test import TestCase

from inspection.models import InspectionBatch, InspectionRecord, Product
from inspection.tasks import process_single_inspection

PREDICT_OK = {
    "predicted_class": "Normal",
    "confidence": 0.9,
    "defect_probability": 0.1,
    "model_version": "test-model",
}


class ProcessSingleInspectionTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="engineer",
            password="test-password",
        )
        self.product = Product.objects.create(product_id="P-PLATE01")
        self.batch = InspectionBatch.objects.create(
            product=self.product, submitted_by=self.user
        )

    def _record(self, name, image="available.png"):
        return InspectionRecord.objects.create(
            batch=self.batch,
            submitted_by=self.user,
            original_filename=name,
            image=image,
        )

    def test_record_lifecycle_queued_to_completed(self):
        record = self._record("ok.png")
        with patch("inspection.tasks.run_predict", return_value=PREDICT_OK):
            result = process_single_inspection.apply(args=[record.pk], throw=True).get()

        record.refresh_from_db()
        self.batch.refresh_from_db()
        self.assertEqual(record.status, InspectionRecord.Status.COMPLETED)
        self.assertEqual(record.predicted_class, "Normal")
        self.assertEqual(self.batch.status, InspectionBatch.Status.COMPLETED)
        self.assertEqual(result["status"], InspectionRecord.Status.COMPLETED)

    def test_image_path_failure_marks_record_failed(self):
        record = self._record("unavailable.png", image="unavailable.png")
        with patch.object(
            FieldFile,
            "path",
            new_callable=PropertyMock,
            side_effect=OSError("storage unavailable"),
        ):
            process_single_inspection.apply(args=[record.pk], throw=True).get()

        record.refresh_from_db()
        self.batch.refresh_from_db()
        self.assertEqual(record.status, InspectionRecord.Status.FAILED)
        self.assertIn("storage unavailable", record.failure_reason)
        self.assertEqual(self.batch.status, InspectionBatch.Status.FAILED)

    def test_one_failed_item_does_not_stop_the_others(self):
        """Per-record dispatch: each task stands alone; batch aggregates."""
        failed_record = self._record("unavailable.png", image="unavailable.png")
        valid_record = self._record("available.png")

        with (
            patch.object(
                FieldFile,
                "path",
                new_callable=PropertyMock,
                side_effect=[OSError("storage unavailable"), "available.png"],
            ),
            patch("inspection.tasks.run_predict", return_value=PREDICT_OK) as predict,
        ):
            process_single_inspection.apply(args=[failed_record.pk], throw=True).get()
            process_single_inspection.apply(args=[valid_record.pk], throw=True).get()

        failed_record.refresh_from_db()
        valid_record.refresh_from_db()
        self.batch.refresh_from_db()

        self.assertEqual(failed_record.status, InspectionRecord.Status.FAILED)
        self.assertIn("storage unavailable", failed_record.failure_reason)
        self.assertEqual(valid_record.status, InspectionRecord.Status.COMPLETED)
        self.assertEqual(valid_record.predicted_class, "Normal")
        self.assertEqual(self.batch.status, InspectionBatch.Status.PARTIAL)
        predict.assert_called_once_with("available.png")

    def test_dispatched_while_other_items_pending_batch_reads_processing(self):
        first = self._record("first.png")
        second = self._record("second.png")
        with patch("inspection.tasks.run_predict", return_value=PREDICT_OK):
            process_single_inspection.apply(args=[first.pk], throw=True).get()

        first.refresh_from_db()
        second.refresh_from_db()
        self.batch.refresh_from_db()
        self.assertEqual(first.status, InspectionRecord.Status.COMPLETED)
        self.assertEqual(second.status, InspectionRecord.Status.QUEUED)
        # Still one item pending, so the batch must not look finished.
        self.assertEqual(self.batch.status, InspectionBatch.Status.PROCESSING)

        with patch("inspection.tasks.run_predict", return_value=PREDICT_OK):
            process_single_inspection.apply(args=[second.pk], throw=True).get()
        self.batch.refresh_from_db()
        self.assertEqual(self.batch.status, InspectionBatch.Status.COMPLETED)

    def test_already_processed_record_is_skipped(self):
        record = self._record("done.png")
        record.status = InspectionRecord.Status.COMPLETED
        record.predicted_class = "Normal"
        record.confidence = 0.9
        record.defect_probability = 0.1
        record.model_version = "test-model"
        record.save()

        with patch("inspection.tasks.run_predict") as predict:
            result = process_single_inspection.apply(args=[record.pk], throw=True).get()

        predict.assert_not_called()
        self.assertTrue(result["skipped"])
        record.refresh_from_db()
        self.assertEqual(record.status, InspectionRecord.Status.COMPLETED)

    def test_validation_failed_record_never_touches_the_model(self):
        record = InspectionRecord.objects.create(
            batch=self.batch,
            submitted_by=self.user,
            original_filename="fake.jpg",
            status=InspectionRecord.Status.FAILED,
            failure_reason="File is not a valid decoded JPEG or PNG image.",
        )
        with patch("inspection.tasks.run_predict") as predict:
            process_single_inspection.apply(args=[record.pk], throw=True).get()

        predict.assert_not_called()
        record.refresh_from_db()
        self.assertEqual(record.status, InspectionRecord.Status.FAILED)
        self.assertEqual(record.failure_reason, "File is not a valid decoded JPEG or PNG image.")
