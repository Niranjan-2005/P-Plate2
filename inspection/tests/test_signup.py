from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from inspection.models import UserProfile

User = get_user_model()


class EngineerSignupTests(TestCase):
    def setUp(self):
        self.url = reverse("web:signup")
        self.client = Client()

    def test_get_renders_engineer_signup_page(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Sign up as Engineer")

    def test_signup_creates_engineer_and_logs_in(self):
        response = self.client.post(
            self.url,
            {"username": "newbie", "password": "secret123", "confirm_password": "secret123"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("web:dashboard"))

        user = User.objects.get(username="newbie")
        self.assertTrue(user.check_password("secret123"))
        self.assertEqual(user.profile.role, UserProfile.Role.ENGINEER)
        # Signed in already: the dashboard is reachable without re-login.
        self.assertEqual(self.client.get(reverse("web:dashboard")).status_code, 200)

    def test_created_engineer_can_log_in_next_time(self):
        self.client.post(
            self.url,
            {"username": "reuser", "password": "pw123456", "confirm_password": "pw123456"},
        )
        self.client.logout()
        response = self.client.post(
            reverse("web:login"), {"username": "reuser", "password": "pw123456"}
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("web:dashboard"))

    def test_password_mismatch_is_rejected(self):
        response = self.client.post(
            self.url,
            {"username": "mismatch", "password": "aaaa1111", "confirm_password": "bbbb2222"},
        )
        self.assertContains(response, "do not match")
        self.assertFalse(User.objects.filter(username="mismatch").exists())

    def test_short_password_is_rejected(self):
        response = self.client.post(
            self.url, {"username": "shorty", "password": "abc", "confirm_password": "abc"}
        )
        self.assertContains(response, "at least")
        self.assertFalse(User.objects.filter(username="shorty").exists())

    def test_duplicate_username_is_rejected(self):
        User.objects.create_user("taken", password="whatever1")
        response = self.client.post(
            self.url,
            {"username": "taken", "password": "secret123", "confirm_password": "secret123"},
        )
        self.assertContains(response, "already taken")
        self.assertEqual(User.objects.filter(username="taken").count(), 1)

    def test_authenticated_user_is_redirected_away(self):
        user = User.objects.create_user("existing", password="secret123")
        UserProfile.objects.create(user=user, role=UserProfile.Role.ENGINEER)
        self.client.force_login(user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("web:dashboard"))
