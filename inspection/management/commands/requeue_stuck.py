"""Re-dispatch records stuck in ``queued``.

When the Celery broker (Redis) is unreachable at upload time, records can be
saved as ``queued`` but never enqueued, so their batch appears to never update.
Once the broker and worker are back, this command re-dispatches those records
and re-aggregates the affected batches.
"""
from django.core.management.base import BaseCommand

from inspection.models import InspectionBatch, InspectionRecord
from inspection.tasks import enqueue_record


class Command(BaseCommand):
    help = (
        "Re-dispatch inspection records still in 'queued' (e.g. the broker was "
        "down at submission time) and refresh their batches."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--batch",
            type=int,
            help="Only requeue records belonging to this batch id.",
        )

    def handle(self, *args, **options):
        qs = InspectionRecord.objects.filter(status=InspectionRecord.Status.QUEUED)
        if options.get("batch"):
            qs = qs.filter(batch_id=options["batch"])

        stuck = list(qs)
        if not stuck:
            self.stdout.write(self.style.SUCCESS("No queued records found."))
            return

        batch_ids = sorted({r.batch_id for r in stuck})
        dispatched = sum(1 for r in stuck if enqueue_record(r))

        for batch_id in batch_ids:
            InspectionBatch.objects.get(pk=batch_id).refresh_status(save=True)

        failed = len(stuck) - dispatched
        self.stdout.write(
            self.style.SUCCESS(
                f"Requeued {dispatched}/{len(stuck)} record(s) across "
                f"{len(batch_ids)} batch(es)."
                + (f" {failed} still failed to dispatch (broker down?)." if failed else "")
            )
        )
