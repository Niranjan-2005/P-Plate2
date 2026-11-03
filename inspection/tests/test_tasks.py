from io import StringIO
from unittest.mock import PropertyMock, patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db.models.fields.files import FieldFile
from django.test import TestCase

from inspection.models import InspectionBatch, InspectionRecord, Product
from inspection.tasks import enqueue_record, process_single_inspection

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


class EnqueueAndRequeueTests(TestCase):
    """Broker-failure handling and the requeue_stuck recovery command."""

    def setUp(self):
        self.user = get_user_model().objects.create_user("eng", password="x")
        self.product = Product.objects.create(product_id="P-PLATE01")
        self.batch = InspectionBatch.objects.create(
            product=self.product, submitted_by=self.user
        )

    def _record(self, name="plate.png"):
        return InspectionRecord.objects.create(
            batch=self.batch,
            submitted_by=self.user,
            original_filename=name,
            image="available.png",
            status=InspectionRecord.Status.QUEUED,
        )

    def test_enqueue_record_returns_task_id_on_success(self):
        record = self._record()
        with patch("inspection.tasks.process_single_inspection.delay") as delay:
            delay.return_value.id = "abc123"
            self.assertEqual(enqueue_record(record), "abc123")
        record.refresh_from_db()
        self.assertEqual(record.status, InspectionRecord.Status.QUEUED)

    def test_enqueue_record_marks_failed_when_broker_down(self):
        record = self._record()
        with patch(
            "inspection.tasks.process_single_inspection.delay",
            side_effect=OSError("Connection refused"),
        ):
            self.assertIsNone(enqueue_record(record))
        record.refresh_from_db()
        self.assertEqual(record.status, InspectionRecord.Status.FAILED)
        self.assertIn("requeue_stuck", record.failure_reason)

    def test_requeue_stuck_command_redispatches_queued_records(self):
        self._record("a.png")
        self._record("b.png")
        out = StringIO()
        with patch("inspection.tasks.process_single_inspection.delay") as delay:
            delay.return_value.id = "t"
            call_command("requeue_stuck", stdout=out)
        # Both stuck records were handed back to the broker.
        self.assertEqual(delay.call_count, 2)
        self.assertIn("Requeued 2/2", out.getvalue())

    def test_requeue_stuck_marks_failed_when_broker_still_down(self):
        self._record("a.png")
        out = StringIO()
        with patch(
            "inspection.tasks.process_single_inspection.delay",
            side_effect=OSError("Connection refused"),
        ):
            call_command("requeue_stuck", stdout=out)
        self.assertIn("Requeued 0/1", out.getvalue())
        self.assertEqual(
            InspectionRecord.objects.filter(
                status=InspectionRecord.Status.FAILED
            ).count(),
            1,
        )

    def test_requeue_stuck_is_noop_when_nothing_queued(self):
        out = StringIO()
        call_command("requeue_stuck", stdout=out)
        self.assertIn("No queued records", out.getvalue())
