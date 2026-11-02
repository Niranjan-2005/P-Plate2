"""Seed the audit trail from reviews that were recorded before ReviewEvent existed."""

from django.db import migrations


def backfill_review_events(apps, schema_editor):
    InspectionRecord = apps.get_model("inspection", "InspectionRecord")
    ReviewEvent = apps.get_model("inspection", "ReviewEvent")

    records = InspectionRecord.objects.exclude(reviewed_class="").exclude(
        reviewer__isnull=True
    )
    for record in records.iterator():
        event = ReviewEvent.objects.create(
            record=record,
            reviewer=record.reviewer,
            previous_class="",
            reviewed_class=record.reviewed_class,
            comment=record.review_comment,
            source="web",
        )
        # Keep the original decision time instead of the migration timestamp.
        ReviewEvent.objects.filter(pk=event.pk).update(
            created_at=record.review_timestamp
        )


def remove_backfilled_events(apps, schema_editor):
    InspectionRecord = apps.get_model("inspection", "InspectionRecord")
    ReviewEvent = apps.get_model("inspection", "ReviewEvent")

    for record in InspectionRecord.objects.exclude(reviewed_class=""):
        ReviewEvent.objects.filter(
            record=record,
            previous_class="",
            reviewed_class=record.reviewed_class,
            comment=record.review_comment,
        ).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("inspection", "0002_reviewevent"),
    ]

    operations = [
        migrations.RunPython(backfill_review_events, remove_backfilled_events),
    ]
