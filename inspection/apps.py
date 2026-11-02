from django.apps import AppConfig


class InspectionConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "inspection"
    verbose_name = "AI Visual Quality Inspection"

    def ready(self):
        from . import signals  # noqa: F401
