from io import BytesIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase
from django.urls import reverse
from PIL import Image
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from inspection.models import (
    InspectionBatch,
    InspectionRecord,
    Product,
    ReviewEvent,
    UserProfile,
)

User = get_user_model()


def _png():
    buf = BytesIO()
    Image.new("RGB", (128, 128), (10, 10, 10)).save(buf, format="PNG")
    return buf.getvalue()


class ReviewGuardTests(TestCase):
    def setUp(self):
        self.product = Product.objects.create(product_id="P-PLATE01")
        self.engineer = User.objects.create_user("eng", password="x")
        UserProfile.objects.create(user=self.engineer, role=UserProfile.Role.ENGINEER)
        self.supervisor = User.objects.create_user("sup", password="x")
        UserProfile.objects.create(user=self.supervisor, role=UserProfile.Role.SUPERVISOR)
        self.admin = User.objects.create_user("adm", password="x")
        UserProfile.objects.create(user=self.admin, role=UserProfile.Role.ADMINISTRATOR)

        self.batch = InspectionBatch.objects.create(
            product=self.product,
            submitted_by=self.engineer,
        )
        self.record = InspectionRecord.objects.create(
            batch=self.batch,
            submitted_by=self.engineer,
            original_filename="plate.png",
            status=InspectionRecord.Status.COMPLETED,
            predicted_class=InspectionRecord.Label.NORMAL,
            confidence=0.91,
            defect_probability=0.09,
            model_version="BIT-PLATE-CV-1.0",
        )
        self.url = reverse("record-review", kwargs={"pk": self.record.pk})
        self.client = APIClient()

    def test_engineer_receives_http_forbidden(self):
        token, _ = Token.objects.get_or_create(user=self.engineer)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
        response = self.client.post(
            self.url,
            {"reviewed_class": "Defective", "review_comment": "override"},
            format="json",
        )
        self.assertEqual(response.status_code, 403)
        self.record.refresh_from_db()
        self.assertEqual(self.record.predicted_class, "Normal")
        self.assertEqual(self.record.reviewed_class, "")

    def test_supervisor_can_review_without_touching_model_output(self):
        token, _ = Token.objects.get_or_create(user=self.supervisor)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
        response = self.client.post(
            self.url,
            {"reviewed_class": "Defective", "review_comment": "scratch visible"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.record.refresh_from_db()
        self.assertEqual(self.record.predicted_class, "Normal")
        self.assertEqual(self.record.reviewed_class, "Defective")
        self.assertEqual(self.record.reviewer_id, self.supervisor.id)
        self.assertEqual(self.record.model_version, "BIT-PLATE-CV-1.0")

    def test_administrator_cannot_review(self):
        token, _ = Token.objects.get_or_create(user=self.admin)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
        response = self.client.post(
            self.url,
            {"reviewed_class": "Defective"},
            format="json",
        )
        self.assertEqual(response.status_code, 403)

    def test_immutable_model_fields_on_save(self):
        self.record.predicted_class = InspectionRecord.Label.DEFECTIVE
        self.record.confidence = 0.01
        self.record.save()
        self.record.refresh_from_db()
        self.assertEqual(self.record.predicted_class, "Normal")
        self.assertEqual(self.record.confidence, 0.91)


class UploadValidationTests(TestCase):
    def setUp(self):
        Product.objects.create(product_id="P-PLATE01")
        self.engineer = User.objects.create_user("eng2", password="x")
        UserProfile.objects.create(user=self.engineer, role=UserProfile.Role.ENGINEER)
        self.token, _ = Token.objects.get_or_create(user=self.engineer)
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {self.token.key}")
        self.url = reverse("batch-upload")

    @patch("inspection.views.process_single_inspection.delay")
    def test_mixed_batch_keeps_valid_items_and_logs_failures(self, delay_mock):
        delay_mock.return_value.id = "test-task-id"
        valid = SimpleUploadedFile("ok.png", _png(), content_type="image/png")
        invalid = SimpleUploadedFile(
            "not_an_image.jpg",
            b"nope",
            content_type="image/jpeg",
        )
        response = self.client.post(
            self.url,
            {"images": [valid, invalid], "product_id": "P-PLATE01"},
            format="multipart",
        )
        self.assertEqual(response.status_code, 201)
        payload = response.json()
        reasons = {item["original_filename"]: item for item in payload["records"]}
        self.assertEqual(reasons["not_an_image.jpg"]["status"], "failed")
        self.assertTrue(reasons["not_an_image.jpg"]["failure_reason"])
        self.assertEqual(reasons["ok.png"]["status"], "queued")
        self.assertEqual(reasons["ok.png"]["predicted_class"], "")

    def test_all_invalid_batch_is_marked_failed_not_queued(self):
        invalid = SimpleUploadedFile("bad.jpg", b"nope", content_type="image/jpeg")
        response = self.client.post(
            self.url,
            {"images": [invalid], "product_id": "P-PLATE01"},
            format="multipart",
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["status"], InspectionBatch.Status.FAILED)

    def test_product_must_be_selected(self):
        valid = SimpleUploadedFile("ok.png", _png(), content_type="image/png")
        response = self.client.post(self.url, {"images": [valid]}, format="multipart")
        self.assertEqual(response.status_code, 400)
        self.assertIn("product_id", response.json()["detail"])


class AuditTrailTests(TestCase):
    """Every supervisor decision is kept; the model output stays untouched."""

    def setUp(self):
        self.product = Product.objects.create(product_id="P-PLATE01")
        self.engineer = User.objects.create_user("owner", password="x")
        UserProfile.objects.create(user=self.engineer, role=UserProfile.Role.ENGINEER)
        self.outsider = User.objects.create_user("other", password="x")
        UserProfile.objects.create(user=self.outsider, role=UserProfile.Role.ENGINEER)
        self.supervisor = User.objects.create_user("boss", password="x")
        UserProfile.objects.create(user=self.supervisor, role=UserProfile.Role.SUPERVISOR)
        self.batch = InspectionBatch.objects.create(
            product=self.product, submitted_by=self.engineer
        )
        self.record = InspectionRecord.objects.create(
            batch=self.batch,
            submitted_by=self.engineer,
            original_filename="plate.png",
            status=InspectionRecord.Status.COMPLETED,
            predicted_class=InspectionRecord.Label.NORMAL,
            confidence=0.9,
            defect_probability=0.1,
            model_version="BIT-PLATE-CV-1.0",
        )
        self.url = reverse("record-review", kwargs={"pk": self.record.pk})

    def _api_client(self, user):
        client = APIClient()
        token, _ = Token.objects.get_or_create(user=user)
        client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
        return client

    def test_each_review_is_appended_to_the_trail(self):
        client = self._api_client(self.supervisor)
        self.assertEqual(
            client.post(
                self.url,
                {"reviewed_class": "Defective", "review_comment": "scratch"},
                format="json",
            ).status_code,
            200,
        )
        self.assertEqual(
            client.post(
                self.url,
                {"reviewed_class": "Normal", "review_comment": "re-checked"},
                format="json",
            ).status_code,
            200,
        )

        events = list(ReviewEvent.objects.filter(record=self.record).order_by("id"))
        self.assertEqual([e.reviewed_class for e in events], ["Defective", "Normal"])
        self.assertEqual([e.previous_class for e in events], ["", "Defective"])
        self.assertEqual(events[1].comment, "re-checked")
        self.assertEqual(events[0].source, ReviewEvent.Source.API)

        self.record.refresh_from_db()
        self.assertEqual(self.record.predicted_class, "Normal")
        self.assertEqual(self.record.confidence, 0.9)
        self.assertEqual(self.record.reviewed_class, "Normal")
        self.assertEqual(self.record.reviewer_id, self.supervisor.id)

    def test_review_events_are_append_only(self):
        self._api_client(self.supervisor).post(
            self.url, {"reviewed_class": "Defective"}, format="json"
        )
        event = ReviewEvent.objects.get(record=self.record)
        event.reviewed_class = "Normal"
        with self.assertRaises(ValidationError):
            event.save()

    def test_review_chain_is_serialised_with_the_record(self):
        self._api_client(self.supervisor).post(
            self.url, {"reviewed_class": "Defective", "review_comment": "pit"},
            format="json",
        )
        payload = self._api_client(self.supervisor).get(self.url).json()
        self.assertEqual(len(payload), 1)
        self.assertEqual(payload[0]["reviewed_class"], "Defective")
        self.assertEqual(payload[0]["reviewer"]["username"], "boss")

    def test_chain_readable_by_owner_only(self):
        self._api_client(self.supervisor).post(
            self.url, {"reviewed_class": "Defective"}, format="json"
        )
        self.assertEqual(
            self._api_client(self.engineer).get(self.url).status_code, 200
        )
        self.assertEqual(
            self._api_client(self.outsider).get(self.url).status_code, 403
        )

    def test_engineer_denial_is_reported_as_json(self):
        response = self._api_client(self.engineer).post(
            self.url, {"reviewed_class": "Defective"}, format="json"
        )
        self.assertEqual(response.status_code, 403)
        self.assertIn("application/json", response["Content-Type"])
        self.assertIn("Engineers", response.json()["detail"])
        self.assertEqual(ReviewEvent.objects.filter(record=self.record).count(), 0)


class WebRoleScopeTests(TestCase):
    """The web layer must enforce exactly the same scopes as the API."""

    def setUp(self):
        self.product = Product.objects.create(product_id="P-PLATE01")
        self.engineer = User.objects.create_user("web-eng", password="x")
        UserProfile.objects.create(user=self.engineer, role=UserProfile.Role.ENGINEER)
        self.supervisor = User.objects.create_user("web-sup", password="x")
        UserProfile.objects.create(user=self.supervisor, role=UserProfile.Role.SUPERVISOR)
        self.admin = User.objects.create_user("web-adm", password="x")
        UserProfile.objects.create(user=self.admin, role=UserProfile.Role.ADMINISTRATOR)
        self.batch = InspectionBatch.objects.create(
            product=self.product, submitted_by=self.engineer
        )
        self.record = InspectionRecord.objects.create(
            batch=self.batch,
            submitted_by=self.engineer,
            original_filename="plate.png",
            status=InspectionRecord.Status.COMPLETED,
            predicted_class=InspectionRecord.Label.NORMAL,
            confidence=0.8,
            defect_probability=0.2,
            model_version="BIT-PLATE-CV-1.0",
        )
        self.client = Client()

    def test_administrator_is_scoped_out_of_inspection_pages(self):
        self.client.force_login(self.admin)
        for name in ("web:history", "web:audit"):
            response = self.client.get(reverse(name))
            self.assertEqual(response.status_code, 302, name)
            self.assertNotIn("/review/", response["Location"])

        response = self.client.get(reverse("web:review"))
        self.assertEqual(response["Location"], reverse("web:dashboard"))

    def test_administrator_dashboard_reports_product_only_scope(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse("web:dashboard"))
        self.assertContains(response, "Administrators manage products")
        self.assertEqual(response.context["total_completed"], 0)

    def test_engineer_cannot_review_through_the_web_form(self):
        self.client.force_login(self.engineer)
        response = self.client.post(
            reverse("web:review-submit", args=[self.record.pk]),
            {"reviewed_class": "Defective", "next": reverse("web:review")},
        )
        self.assertEqual(response["Location"], reverse("web:dashboard"))
        self.record.refresh_from_db()
        self.assertEqual(self.record.reviewed_class, "")
        self.assertEqual(ReviewEvent.objects.count(), 0)

    def test_supervisor_amendment_keeps_the_earlier_decision(self):
        self.client.force_login(self.supervisor)
        submit = reverse("web:review-submit", args=[self.record.pk])
        self.client.post(submit, {"reviewed_class": "Defective", "review_comment": "pit"})
        self.client.post(submit, {"reviewed_class": "Normal", "review_comment": "lint"})

        events = list(ReviewEvent.objects.filter(record=self.record).order_by("id"))
        self.assertEqual([e.reviewed_class for e in events], ["Defective", "Normal"])
        self.assertEqual([e.source for e in events], ["web", "web"])

        response = self.client.get(reverse("web:audit"))
        self.assertEqual(response.context["total"], 2)

        self.record.refresh_from_db()
        self.assertEqual(self.record.predicted_class, "Normal")
        self.assertEqual(self.record.review_comment, "lint")

    def test_reviewed_records_stay_available_for_correction(self):
        self.client.force_login(self.supervisor)
        self.client.post(
            reverse("web:review-submit", args=[self.record.pk]),
            {"reviewed_class": "Defective"},
        )
        response = self.client.get(reverse("web:review"))
        self.assertEqual(response.context["reviewed_count"], 1)
        self.assertEqual(list(response.context["reviewed_recent"]), [self.record])

    def test_history_review_filters(self):
        self.client.force_login(self.supervisor)
        self.client.post(
            reverse("web:review-submit", args=[self.record.pk]),
            {"reviewed_class": "Normal"},
        )
        url = reverse("web:history")
        self.assertEqual(self.client.get(f"{url}?review=pending").context["total_records"], 0)
        self.assertEqual(self.client.get(f"{url}?review=reviewed").context["total_records"], 1)
        self.assertEqual(self.client.get(f"{url}?review=upheld").context["total_records"], 1)
        self.assertEqual(self.client.get(f"{url}?review=corrected").context["total_records"], 0)
        self.assertEqual(self.client.get(f"{url}?review=nonsense").context["total_records"], 1)

    def test_external_next_url_is_not_followed(self):
        self.client.force_login(self.supervisor)
        response = self.client.post(
            reverse("web:review-submit", args=[self.record.pk]),
            {"reviewed_class": "Defective", "next": "https://evil.example.com/"},
        )
        self.assertEqual(response["Location"], reverse("web:review"))

    def test_all_rejected_web_batch_is_not_left_queued(self):
        self.client.force_login(self.engineer)
        invalid = SimpleUploadedFile("bad.jpg", b"nope", content_type="image/jpeg")
        response = self.client.post(
            reverse("web:upload"),
            {"images": [invalid], "product_id": "P-PLATE01"},
        )
        self.assertEqual(response.status_code, 302)
        # The upload creates its own batch; the setUp batch stays untouched.
        created = InspectionBatch.objects.order_by("-id").first()
        self.assertEqual(created.submitted_by_id, self.engineer.id)
        self.assertEqual(created.records.count(), 1)
        self.assertEqual(created.records.first().status, InspectionRecord.Status.FAILED)
        self.assertEqual(created.status, InspectionBatch.Status.FAILED)


class ProductAndAdminConsoleTests(TestCase):
    """Products belong to administrators; recorded results are never hand-edited."""

    def setUp(self):
        self.admin = User.objects.create_user("adm-prod", password="x")
        UserProfile.objects.create(user=self.admin, role=UserProfile.Role.ADMINISTRATOR)
        self.engineer = User.objects.create_user("eng-prod", password="x")
        UserProfile.objects.create(user=self.engineer, role=UserProfile.Role.ENGINEER)
        self.supervisor = User.objects.create_user("sup-prod", password="x")
        UserProfile.objects.create(user=self.supervisor, role=UserProfile.Role.SUPERVISOR)
        self.product = Product.objects.create(product_id="P-PLATE01")
        self.batch = InspectionBatch.objects.create(
            product=self.product, submitted_by=self.engineer
        )
        self.record = InspectionRecord.objects.create(
            batch=self.batch,
            submitted_by=self.engineer,
            original_filename="plate.png",
            status=InspectionRecord.Status.COMPLETED,
            predicted_class=InspectionRecord.Label.NORMAL,
            confidence=0.7,
            defect_probability=0.3,
            model_version="BIT-PLATE-CV-1.0",
        )

    def _api_client(self, user):
        client = APIClient()
        token, _ = Token.objects.get_or_create(user=user)
        client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
        return client

    def test_only_administrators_manage_products(self):
        url = reverse("product-list")
        for user in (self.engineer, self.supervisor):
            response = self._api_client(user).post(
                url, {"product_id": "P-NOPE01", "name": "Nope"}, format="json"
            )
            self.assertEqual(response.status_code, 403, user.username)

        response = self._api_client(self.admin).post(
            url, {"product_id": "P-NEW01", "name": "New plate"}, format="json"
        )
        self.assertEqual(response.status_code, 201)
        self.assertTrue(Product.objects.filter(product_id="P-NEW01").exists())

    def test_records_are_read_only_in_the_admin_console(self):
        root = User.objects.create_superuser("root-prod", password="x")
        UserProfile.objects.create(user=root, role=UserProfile.Role.ADMINISTRATOR)
        web = Client()
        web.force_login(root)

        change_url = reverse("admin:inspection_inspectionrecord_change", args=[self.record.pk])
        self.assertEqual(web.get(change_url).status_code, 200)
        self.assertEqual(
            web.get(reverse("admin:inspection_inspectionrecord_add")).status_code, 403
        )

        # Django ignores readonly field data on POST, and the model guard
        # protects the prediction, so neither path can alter a result.
        web.post(
            change_url,
            {
                "predicted_class": InspectionRecord.Label.DEFECTIVE,
                "confidence": "0.01",
                "reviewed_class": InspectionRecord.Label.DEFECTIVE,
                "review_comment": "tampering",
            },
        )
        self.record.refresh_from_db()
        self.assertEqual(self.record.predicted_class, "Normal")
        self.assertEqual(self.record.confidence, 0.7)
        self.assertEqual(self.record.reviewed_class, "")
        self.assertEqual(self.record.review_comment, "")

    def test_audit_events_are_view_only_in_the_admin_console(self):
        root = User.objects.create_superuser("root-audit", password="x")
        UserProfile.objects.create(user=root, role=UserProfile.Role.ADMINISTRATOR)
        ReviewEvent.objects.create(
            record=self.record,
            reviewer=self.supervisor,
            previous_class="",
            reviewed_class=InspectionRecord.Label.DEFECTIVE,
            comment="pit confirmed",
            source=ReviewEvent.Source.WEB,
        )
        web = Client()
        web.force_login(root)

        changelist = web.get(reverse("admin:inspection_reviewevent_changelist"))
        self.assertEqual(changelist.status_code, 200)
        self.assertContains(changelist, "pit confirmed")

        event = ReviewEvent.objects.get()
        change_url = reverse("admin:inspection_reviewevent_change", args=[event.pk])
        # Viewing the trail is allowed, but the page exposes no editable inputs
        # and submitting a change is refused.
        detail = web.get(change_url)
        self.assertEqual(detail.status_code, 200)
        self.assertNotIn(b'name="reviewed_class"', detail.content)
        self.assertEqual(
            web.post(change_url, {"reviewed_class": "Normal"}).status_code, 403
        )
        event.refresh_from_db()
        self.assertEqual(event.reviewed_class, InspectionRecord.Label.DEFECTIVE)
        self.assertEqual(event.comment, "pit confirmed")

    def test_non_staff_roles_cannot_open_the_admin_console(self):
        web = Client()
        web.force_login(self.engineer)
        response = web.get(reverse("admin:index"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login/", response["Location"])
