from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from rest_framework.authtoken.models import Token

from inspection.models import Product, UserProfile

User = get_user_model()

DEMO_USERS = (
    ("admin", "administrator", "admin-pass"),
    ("engineer", "engineer", "engineer-pass"),
    ("supervisor", "supervisor", "supervisor-pass"),
)


class Command(BaseCommand):
    help = "Create P-PLATE01 and demo RBAC users (admin, engineer, supervisor)."

    def handle(self, *args, **options):
        product, created = Product.objects.get_or_create(
            product_id="P-PLATE01",
            defaults={
                "name": "Demonstration metal plate",
                "description": "Synthetic centered plate with scratches or pits",
                "is_active": True,
            },
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"{'Created' if created else 'Exists'} product {product.product_id}"
            )
        )

        for username, role, password in DEMO_USERS:
            user, user_created = User.objects.get_or_create(
                username=username,
                defaults={"is_staff": role == "administrator", "is_superuser": role == "administrator"},
            )
            if user_created:
                user.set_password(password)
                user.save()
            profile, _ = UserProfile.objects.get_or_create(user=user, defaults={"role": role})
            if profile.role != role:
                profile.role = role
                profile.save(update_fields=["role"])
            token, _ = Token.objects.get_or_create(user=user)
            self.stdout.write(
                f"{username:12} role={role:16} token={token.key} "
                f"{'(created)' if user_created else '(exists)'}"
            )
