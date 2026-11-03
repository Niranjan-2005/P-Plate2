"""
Server-rendered web UI for the P-PLATE visual inspection platform.

These views reuse the same models, RBAC helpers and validation rules as the
DRF API, so the interface and the API stay consistent. All pages are backed by
real database data (no mock values).
"""

from __future__ import annotations

import json
from datetime import timedelta

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model, login
from django.contrib.auth.decorators import login_required
from django.db.models import Avg, Case, CharField, Count, F, Q, When
from django.db.models.functions import TruncDate
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from . import utils
from .models import (
    InspectionBatch,
    InspectionRecord,
    Product,
    ReviewEvent,
    UserProfile,
    record_review,
)
from .permissions import get_user_role, is_administrator, is_engineer, is_supervisor

User = get_user_model()

MIN_SIGNUP_PASSWORD = 8

# Final operational label = supervisor override when present, else model output.
FINAL_LABEL = Case(
    When(reviewed_class__gt="", then=F("reviewed_class")),
    default=F("predicted_class"),
    output_field=CharField(),
)

NAV_ITEMS = (
    ("dashboard", "Dashboard", "grid"),
    ("upload", "Batch Inspection", "upload"),
    ("results", "Results", "image"),
    ("history", "Inspection History", "history"),
    ("review", "Supervisor Review", "check"),
    ("audit", "Audit Log", "list"),
    ("settings", "Settings", "gear"),
)

# Nav key -> (url name, needs-latest-batch?)
NAV_ROUTES = {
    "dashboard": "web:dashboard",
    "upload": "web:upload",
    "results": "web:results-latest",
    "history": "web:history",
    "review": "web:review",
    "audit": "web:audit",
    "settings": "web:settings",
}


def _role_flags(user):
    return {
        "role": get_user_role(user) or "",
        "role_display": UserProfile.Role(get_user_role(user)).label if get_user_role(user) else "No role",
        "is_engineer": is_engineer(user),
        "is_supervisor": is_supervisor(user),
        "is_admin": is_administrator(user),
    }


def _shell(request, active, **extra):
    """Common context for every page (sidebar, topbar, role badge, model info)."""
    flags = _role_flags(request.user)
    allowed = _visible_nav(flags)
    nav = []
    for key, label, icon in NAV_ITEMS:
        nav.append(
            {
                "key": key,
                "label": label,
                "icon": icon,
                "href": reverse(NAV_ROUTES[key]),
                "allowed": key in allowed,
            }
        )
    context = {
        "active_nav": active,
        "nav": nav,
        "model_version": settings.MODEL_VERSION,
        "default_product": settings.DEFAULT_PRODUCT_ID,
        "today": timezone.localdate(),
    }
    context.update(flags)
    context["nav_allowed"] = allowed
    context.update(extra)
    return context


def _visible_nav(flags):
    """
    Nav entries the user's role may actually open.

    Administrators have read-only oversight: they may open every batch's
    history and results, but review and the audit log stay supervisor-only.
    """
    allowed = {"dashboard", "settings"}
    if flags["is_engineer"]:
        allowed |= {"upload", "history", "results"}
    if flags["is_supervisor"]:
        allowed |= {"history", "results", "review", "audit"}
    if flags["is_admin"]:
        # Read-only oversight: admins may open every batch's history and
        # results, but never review predictions or read the review audit log.
        allowed |= {"history", "results"}
    return allowed


# Self-service signup only ever grants the lowest-privilege inspection role.
# Supervisor/administrator accounts stay provisioned by an administrator.
SIGNUP_ROLE = UserProfile.Role.ENGINEER


def signup(request):
    """Create an engineer account and sign it in immediately.

    The new user gets a UserProfile with the engineer role, so the very same
    credentials work on the normal login form next time.
    """
    if request.user.is_authenticated:
        return redirect("web:dashboard")

    error = None
    username = ""
    if request.method == "POST":
        username = (request.POST.get("username") or "").strip()
        password = request.POST.get("password") or ""
        confirm = request.POST.get("confirm_password") or ""

        if not username or not password:
            error = "Username and password are both required."
        elif len(username) > 150:
            error = "Username is too long."
        elif len(password) < MIN_SIGNUP_PASSWORD:
            error = f"Password must be at least {MIN_SIGNUP_PASSWORD} characters."
        elif password != confirm:
            error = "The two passwords do not match."
        elif User.objects.filter(username__iexact=username).exists():
            error = "That username is already taken."
        else:
            user = User.objects.create_user(username=username, password=password)
            UserProfile.objects.create(user=user, role=SIGNUP_ROLE)
            login(request, user)
            messages.success(
                request,
                "Engineer account created. You can now submit inspection images.",
            )
            return redirect("web:dashboard")

    return render(
        request,
        "registration/signup.html",
        {"error": error, "username": username, "today": timezone.localdate()},
    )


@login_required
def dashboard(request):
    flags = _role_flags(request.user)
    scope = _record_scope(request.user, flags)

    completed = scope.filter(status=InspectionRecord.Status.COMPLETED)
    total_completed = completed.count()
    defective = completed.annotate(final=FINAL_LABEL).filter(final="Defective").count()
    normal = total_completed - defective
    defect_rate = (defective / total_completed * 100) if total_completed else 0.0
    avg_confidence = completed.aggregate(v=Avg("confidence"))["v"] or 0.0

    pending = scope.filter(
        status__in=[
            InspectionRecord.Status.QUEUED,
            InspectionRecord.Status.PROCESSING,
        ]
    ).count()
    failed = scope.filter(status=InspectionRecord.Status.FAILED).count()

    reviewed = scope.filter(reviewed_class__gt="")
    reviewed_count = reviewed.count()
    upheld = reviewed.filter(predicted_class=F("reviewed_class")).count()
    agreement = (upheld / reviewed_count * 100) if reviewed_count else 100.0

    # Daily volume over the last 7 days (completed records, split by final label).
    today = timezone.localdate()
    days = []
    normal_series = []
    defect_series = []

    by_day = {}
    for row in (
        completed.annotate(day=TruncDate("created_at"), final=FINAL_LABEL)
        .values("day", "final")
        .annotate(n=Count("id"))
    ):
        by_day.setdefault(row["day"], {})[row["final"]] = row["n"]
    for offset in range(6, -1, -1):
        day = today - timedelta(days=offset)
        counts = by_day.get(day, {})
        days.append(day.strftime("%d %b"))
        normal_series.append(counts.get("Normal", 0))
        defect_series.append(counts.get("Defective", 0))

    recent_batches = (
        _batch_scope(request.user, flags)
        .select_related("product", "submitted_by")
        .prefetch_related("records")[:6]
    )

    context = _shell(
        request,
        "dashboard",
        scope_note=SCOPE_NOTES.get(flags["role"], ""),
        total_completed=total_completed,
        defective=defective,
        normal=normal,
        defect_rate=defect_rate,
        avg_confidence=avg_confidence * 100,
        pending=pending,
        failed=failed,
        reviewed_count=reviewed_count,
        agreement=agreement,
        chart_days=json.dumps(days),
        chart_normal=json.dumps(normal_series),
        chart_defect=json.dumps(defect_series),
        chart_mix_normal=normal,
        chart_mix_defective=defective,
        recent_batches=recent_batches,
    )
    return render(request, "web/dashboard.html", context)


def _record_scope(user, flags):
    """Supervisors and administrators see everything; engineers only their own."""
    qs = (
        InspectionRecord.objects.select_related(
            "batch__product", "submitted_by", "reviewer"
        )
        .prefetch_related("review_events__reviewer")
    )
    if flags["is_supervisor"] or flags["is_admin"]:
        return qs
    if flags["is_engineer"]:
        return qs.filter(submitted_by=user)
    return qs.none()


def _batch_scope(user, flags):
    qs = (
        InspectionBatch.objects.select_related("product", "submitted_by")
        .prefetch_related("records__review_events__reviewer")
    )
    if flags["is_supervisor"] or flags["is_admin"]:
        return qs
    if flags["is_engineer"]:
        return qs.filter(submitted_by=user)
    return qs.none()


def _may_view_inspection_data(flags):
    return bool(flags["is_engineer"] or flags["is_supervisor"] or flags["is_admin"])


SCOPE_NOTES = {
    UserProfile.Role.ENGINEER: "Metrics cover your own submissions only.",
    UserProfile.Role.SUPERVISOR: "Metrics cover every inspection in the plant.",
    UserProfile.Role.ADMINISTRATOR: (
        "Administrators have read-only oversight of every batch and result; "
        "predictions are reviewed by supervisors."
    ),
}


@login_required
def upload(request):
    flags = _role_flags(request.user)
    if not flags["is_engineer"]:
        messages.error(request, "Only engineers may submit inspection images.")
        return redirect("web:dashboard")

    products = Product.objects.filter(is_active=True)

    if request.method == "POST":
        files = request.FILES.getlist("images")
        product_code = request.POST.get("product_id") or settings.DEFAULT_PRODUCT_ID
        notes = (request.POST.get("notes") or "").strip()[:255]

        size_error = utils.validate_batch_size(len(files))
        if size_error:
            messages.error(request, size_error)
            return render(request, "web/upload.html", _upload_context(request, products))

        product = products.filter(product_id=product_code).first()
        if product is None:
            messages.error(request, f"Unknown or inactive product '{product_code}'.")
            return render(request, "web/upload.html", _upload_context(request, products))

        batch = InspectionBatch.objects.create(
            product=product,
            submitted_by=request.user,
            status=InspectionBatch.Status.QUEUED,
            notes=notes,
        )
        queued = 0
        rejected = 0
        for uploaded in files:
            filename = getattr(uploaded, "name", "unnamed") or "unnamed"
            reason = utils.validate_image_file(uploaded)
            if reason:
                InspectionRecord.objects.create(
                    batch=batch,
                    submitted_by=request.user,
                    original_filename=filename,
                    status=InspectionRecord.Status.FAILED,
                    failure_reason=reason,
                )
                rejected += 1
                continue
            InspectionRecord.objects.create(
                batch=batch,
                submitted_by=request.user,
                original_filename=filename,
                image=uploaded,
                status=InspectionRecord.Status.QUEUED,
            )
            queued += 1

        if queued:
            from .tasks import enqueue_record

            # One Celery task per valid image, matching the challenge flow.
            # Dispatch is failure-safe: if the broker is down the record is
            # marked failed here instead of hanging in "queued" forever.
            pending = list(
                InspectionRecord.objects.filter(
                    batch=batch, status=InspectionRecord.Status.QUEUED
                )
            )
            task_ids = [tid for tid in (enqueue_record(r) for r in pending) if tid]
            batch.celery_task_id = (task_ids[0] if task_ids else "")[:64]
            batch.save(update_fields=["celery_task_id"])
            # Aggregate from record statuses so eager-mode runs (and rejected
            # -only batches) never leave the batch stuck on "queued".
            batch.refresh_status(save=True)
        else:
            # Nothing queued for inference: keep the batch status consistent
            # with its records instead of leaving it stuck on "queued".
            batch.refresh_status(save=True)

        msg = f"Batch #{batch.pk} created with {queued} image(s) queued for inference."
        if rejected:
            msg += f" {rejected} file(s) were rejected by validation."
        messages.success(request, msg)
        return redirect("web:results", pk=batch.pk)

    return render(request, "web/upload.html", _upload_context(request, products))


def _upload_context(request, products):
    return _shell(
        request,
        "upload",
        products=products,
        default_product=settings.DEFAULT_PRODUCT_ID,
        max_batch=utils.MAX_BATCH_SIZE,
        max_size_mib=utils.MAX_FILE_SIZE_BYTES // (1024 * 1024),
        min_dim=utils.MIN_DIMENSION,
        max_dim=utils.MAX_DIMENSION,
    )


@login_required
def results(request, pk):
    flags = _role_flags(request.user)
    batch = _batch_scope(request.user, flags).filter(pk=pk).first()
    if batch is None:
        messages.error(
            request,
            "Batch not found, or it belongs to another engineer. "
            "You can only open your own batches.",
        )
        return redirect("web:history")

    records = list(batch.records.select_related("reviewer").all())
    counts = {
        "total": len(records),
        "completed": sum(r.status == InspectionRecord.Status.COMPLETED for r in records),
        "queued": sum(r.status == InspectionRecord.Status.QUEUED for r in records),
        "processing": sum(r.status == InspectionRecord.Status.PROCESSING for r in records),
        "failed": sum(r.status == InspectionRecord.Status.FAILED for r in records),
        "defective": sum(
            1 for r in records if r.effective_class == InspectionRecord.Label.DEFECTIVE
        ),
    }
    images = [
        {
            "id": r.id,
            "url": r.image.url if r.image else "",
            "name": r.original_filename,
            "label": r.effective_class,
            "status": r.status,
            "confidence": r.confidence,
        }
        for r in records
        if r.image
    ]
    return render(
        request,
        "web/results.html",
        _shell(request, "results", batch=batch, counts=counts, images=images, records=records),
    )


@login_required
def results_latest(request):
    flags = _role_flags(request.user)
    latest = _batch_scope(request.user, flags).first()
    if latest is None:
        return redirect("web:history")
    return redirect("web:results", pk=latest.pk)


REVIEW_FILTERS = (
    ("", "Any review state"),
    ("pending", "Awaiting review"),
    ("reviewed", "Reviewed (any)"),
    ("upheld", "Reviewed and upheld"),
    ("corrected", "Reviewed and corrected"),
)


@login_required
def history(request):
    flags = _role_flags(request.user)
    if not _may_view_inspection_data(flags):
        messages.error(
            request,
            "Inspection history belongs to engineers, supervisors and "
            "administrators; your account has no inspection role.",
        )
        return redirect("web:settings")
    scope = _record_scope(request.user, flags)

    status_filter = request.GET.get("status", "")
    label_filter = request.GET.get("label", "")
    review_filter = request.GET.get("review", "")
    search = request.GET.get("q", "")
    ordering = request.GET.get("order", "-created_at")
    if ordering not in {"created_at", "-created_at", "confidence", "-confidence", "id", "-id"}:
        ordering = "-created_at"
    if review_filter not in {value for value, _ in REVIEW_FILTERS}:
        review_filter = ""

    if status_filter:
        scope = scope.filter(status=status_filter)
    if label_filter:
        scope = scope.annotate(final=FINAL_LABEL).filter(final=label_filter)
    if review_filter == "pending":
        scope = scope.filter(reviewed_class="")
    elif review_filter == "reviewed":
        scope = scope.exclude(reviewed_class="")
    elif review_filter == "upheld":
        scope = scope.exclude(reviewed_class="").filter(predicted_class=F("reviewed_class"))
    elif review_filter == "corrected":
        scope = scope.exclude(reviewed_class="").exclude(
            predicted_class=F("reviewed_class")
        )
    if search:
        scope = scope.filter(Q(original_filename__icontains=search) | Q(batch__product__product_id__icontains=search))

    from django.core.paginator import Paginator

    paginator = Paginator(scope.order_by(ordering), 25)
    page_obj = paginator.get_page(request.GET.get("page"))

    return render(
        request,
        "web/history.html",
        _shell(
            request,
            "history",
            page_obj=page_obj,
            total_records=paginator.count,
            status_filter=status_filter,
            label_filter=label_filter,
            review_filter=review_filter,
            review_filters=REVIEW_FILTERS,
            search=search,
            ordering=ordering,
            statuses=InspectionRecord.Status.choices,
            labels=InspectionRecord.Label.choices,
        ),
    )


@login_required
def review(request):
    flags = _role_flags(request.user)
    if not flags["is_supervisor"]:
        messages.error(request, "Only supervisors may review predictions.")
        return redirect("web:dashboard")

    scope = _record_scope(request.user, flags).filter(
        status=InspectionRecord.Status.COMPLETED
    )
    pending = scope.filter(reviewed_class="").order_by("-created_at")
    # Reviewed records stay reachable so a decision can be amended; every
    # submission is appended to the audit trail, so nothing is lost.
    reviewed = scope.exclude(reviewed_class="").order_by("-review_timestamp", "-id")

    selected_id = request.GET.get("record")
    selected = None
    if selected_id:
        selected = scope.filter(pk=selected_id).first()
    elif pending.exists():
        selected = pending.first()
    elif reviewed.exists():
        selected = reviewed.first()

    return render(
        request,
        "web/review.html",
        _shell(
            request,
            "review",
            selected=selected,
            queue=pending.order_by("-created_at")[:20],
            reviewed_recent=reviewed[:20],
            pending_count=pending.count(),
            reviewed_count=reviewed.count(),
            can_review=flags["is_supervisor"],
            labels=InspectionRecord.Label.choices,
        ),
    )


def _safe_review_redirect(request):
    """
    Follow the form's hidden `next` only when it points back into this site,
    so a crafted POST cannot bounce a supervisor to an external host.
    """
    target = (request.POST.get("next") or "").strip()
    if target and url_has_allowed_host_and_scheme(
        target, allowed_hosts={request.get_host()}
    ):
        return target
    return reverse("web:review")


@require_POST
@login_required
def review_submit(request, pk):
    flags = _role_flags(request.user)
    if not flags["is_supervisor"]:
        if flags["is_engineer"]:
            messages.error(
                request,
                "Engineers are not permitted to review or correct predictions.",
            )
        else:
            messages.error(
                request,
                "Only supervisors may review predictions; administrators manage "
                "products.",
            )
        return redirect("web:dashboard")

    record = get_object_or_404(InspectionRecord, pk=pk)
    reviewed_class = request.POST.get("reviewed_class", "")
    comment = (request.POST.get("review_comment") or "").strip()

    if reviewed_class not in InspectionRecord.Label.values:
        messages.error(request, "Invalid review label.")
    elif record.status != InspectionRecord.Status.COMPLETED:
        messages.error(request, "Only completed inferences can be reviewed.")
    else:
        was_reviewed = bool(record.reviewed_class)
        record_review(
            record=record,
            reviewer=request.user,
            reviewed_class=reviewed_class,
            comment=comment,
            source=ReviewEvent.Source.WEB,
        )
        messages.success(
            request,
            f"Record #{record.pk} marked as {reviewed_class}"
            + (
                " (amended; the earlier decision is kept in the audit log)"
                if was_reviewed
                else ""
            )
            + ".",
        )

    return redirect(_safe_review_redirect(request))


@login_required
def audit(request):
    flags = _role_flags(request.user)
    if not flags["is_supervisor"]:
        messages.error(request, "Only supervisors may view the audit log.")
        return redirect("web:dashboard")

    events = ReviewEvent.objects.select_related(
        "record", "record__batch__product", "record__submitted_by", "reviewer"
    )
    record_id = request.GET.get("record", "")
    if record_id.isdigit():
        events = events.filter(record_id=int(record_id))
    else:
        record_id = ""

    from django.core.paginator import Paginator

    paginator = Paginator(events.order_by("-created_at", "-id"), 25)
    page_obj = paginator.get_page(request.GET.get("page"))
    return render(
        request,
        "web/audit.html",
        _shell(
            request,
            "audit",
            page_obj=page_obj,
            total=paginator.count,
            selected_record=record_id,
        ),
    )


@login_required
def settings_page(request):
    flags = _role_flags(request.user)
    metadata = {}
    try:
        metadata = json.loads(
            (settings.BASE_DIR / "Model" / "model_metadata.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        metadata = {}
    # Administrators manage the full catalogue; other roles only ever see the
    # product lines they may upload against.
    products = (
        Product.objects.all()
        if flags["is_admin"]
        else Product.objects.filter(is_active=True)
    )
    return render(
        request,
        "web/settings.html",
        _shell(request, "settings", metadata=metadata, products=products, scope_note=SCOPE_NOTES.get(flags["role"], "")),
    )
