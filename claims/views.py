from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, Q
from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from accounts.forms import UserRoleForm
from accounts.models import User

from .ai.pipeline import maybe_auto_process, process_claim
from .ai.review import HUMAN_DECIDED, ReviewError, apply_review, changed_fields
from .forms import (
    ClaimForm,
    DocumentUploadForm,
    MessageForm,
    ReviewForm,
    StatusUpdateForm,
)
from .models import Claim, ClaimDocument, ExtractedClaim
from .notifications import notify_new_message, notify_status_change
from .permissions import (
    admin_required,
    claims_visible_to,
    get_claim_for_user,
    staff_required,
)


def _save_documents(claim, files):
    for f in files:
        ClaimDocument.objects.create(claim=claim, file=f, original_name=f.name[:255])


# ---------- Claimant-facing ----------


@login_required
def dashboard(request):
    qs = claims_visible_to(request.user)
    status = request.GET.get("status", "")
    date_from = request.GET.get("from", "")
    date_to = request.GET.get("to", "")
    q = request.GET.get("q", "").strip()

    if status in Claim.Status.values:
        qs = qs.filter(status=status)
    if date_from:
        qs = qs.filter(incident_date__gte=date_from)
    if date_to:
        qs = qs.filter(incident_date__lte=date_to)
    if q:
        qs = qs.filter(Q(claim_number__icontains=q) | Q(policy_number__icontains=q))

    counts = {
        row["status"]: row["n"]
        for row in claims_visible_to(request.user).values("status").annotate(n=Count("id"))
    }
    page = Paginator(qs, 10).get_page(request.GET.get("page"))
    params = request.GET.copy()
    params.pop("page", None)
    return render(
        request,
        "claims/dashboard.html",
        {
            "page": page,
            "statuses": Claim.Status.choices,
            "counts": counts,
            "total": sum(counts.values()),
            "filters": {"status": status, "from": date_from, "to": date_to, "q": q},
            "query_string": params.urlencode(),
        },
    )


@login_required
def claim_create(request):
    if not request.user.is_claimant:
        messages.error(request, "Only claimants can submit claims.")
        return redirect("claims:dashboard")
    form = ClaimForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            claim = form.save(commit=False)
            claim.claimant = request.user
            claim.save()
            _save_documents(claim, form.cleaned_data["documents"])
        notify_status_change(claim)
        maybe_auto_process(claim)
        messages.success(request, f"Claim {claim.claim_number} submitted.")
        return redirect("claims:detail", pk=claim.pk)
    return render(request, "claims/claim_form.html", {"form": form})


@login_required
def claim_detail(request, pk):
    claim = get_claim_for_user(request.user, pk)
    msg_form = MessageForm()
    status_form = None
    if request.user.is_staff_role:
        status_form = StatusUpdateForm(
            initial={"status": claim.status, "assigned_agent": claim.assigned_agent}
        )
    return render(
        request,
        "claims/claim_detail.html",
        {
            "claim": claim,
            "documents": claim.documents.all(),
            "history": claim.history.select_related("changed_by"),
            "thread": claim.messages.select_related("author"),
            "msg_form": msg_form,
            "status_form": status_form,
            "upload_form": DocumentUploadForm(),
        },
    )


@login_required
def claim_add_message(request, pk):
    claim = get_claim_for_user(request.user, pk)
    if request.method == "POST":
        form = MessageForm(request.POST)
        if form.is_valid():
            msg = form.save(commit=False)
            msg.claim, msg.author = claim, request.user
            msg.save()
            notify_new_message(claim, msg)
            messages.success(request, "Message sent.")
    return redirect("claims:detail", pk=claim.pk)


@login_required
def claim_add_documents(request, pk):
    claim = get_claim_for_user(request.user, pk)
    if request.method == "POST":
        form = DocumentUploadForm(request.POST, request.FILES)
        if form.is_valid():
            _save_documents(claim, form.cleaned_data["documents"])
            maybe_auto_process(claim)
            messages.success(request, "Documents uploaded.")
        else:
            for err in form.errors.get("documents", []):
                messages.error(request, err)
    return redirect("claims:detail", pk=claim.pk)


@login_required
def document_download(request, pk):
    """Serve uploads only to users allowed to see the parent claim."""
    doc = get_object_or_404(ClaimDocument, pk=pk)
    get_claim_for_user(request.user, doc.claim_id)  # 404 if not permitted
    try:
        return FileResponse(doc.file.open("rb"), as_attachment=False, filename=doc.original_name)
    except FileNotFoundError:
        raise Http404


# ---------- Staff actions ----------


@staff_required
def claim_update_status(request, pk):
    claim = get_claim_for_user(request.user, pk)
    if request.method == "POST":
        form = StatusUpdateForm(request.POST)
        if form.is_valid():
            new_status = form.cleaned_data["status"]
            note = form.cleaned_data["note"]
            agent = form.cleaned_data["assigned_agent"]
            # Only admins may (re)assign; agents can only act on claims they can see.
            if request.user.is_admin_role and agent != claim.assigned_agent:
                claim.assigned_agent = agent
                claim.save(update_fields=["assigned_agent", "updated_at"])
            changed = claim.change_status(new_status, request.user, note)
            if changed:
                notify_status_change(claim, note)
                messages.success(request, f"Status updated to {claim.get_status_display()}.")
            else:
                messages.info(request, "Status unchanged.")
    return redirect("claims:detail", pk=claim.pk)


# ---------- Admin panel ----------


@admin_required
def panel_home(request):
    claim_counts = {
        r["status"]: r["n"] for r in Claim.objects.values("status").annotate(n=Count("id"))
    }
    return render(
        request,
        "admin_panel/home.html",
        {
            "claim_counts": claim_counts,
            "total_claims": sum(claim_counts.values()),
            "user_count": User.objects.count(),
            "recent": Claim.objects.select_related("claimant")[:8],
        },
    )


@admin_required
def panel_users(request):
    users = User.objects.order_by("username")
    role = request.GET.get("role")
    if role in User.Role.values:
        users = users.filter(role=role)
    return render(
        request,
        "admin_panel/users.html",
        {"users": users, "roles": User.Role.choices, "role": role or ""},
    )


@admin_required
def panel_user_edit(request, pk):
    target = get_object_or_404(User, pk=pk)
    form = UserRoleForm(request.POST or None, instance=target)
    if request.method == "POST" and form.is_valid():
        if target == request.user and (
            form.cleaned_data["role"] != User.Role.ADMIN or not form.cleaned_data["is_active"]
        ) and not request.user.is_superuser:
            messages.error(request, "You can't demote or deactivate your own account.")
        else:
            form.save()
            messages.success(request, f"Updated {target.username}.")
            return redirect("claims:panel_users")
    return render(request, "admin_panel/user_edit.html", {"form": form, "target": target})


# ---------- AI extraction review (staff) ----------

REVIEW_TABS = [
    (ExtractedClaim.Status.NEEDS_REVIEW, "Needs review"),
    (ExtractedClaim.Status.AUTO_VERIFIED, "Passed automatic checks"),
    (ExtractedClaim.Status.VERIFIED, "Approved"),
    (ExtractedClaim.Status.REJECTED, "Rejected"),
    (ExtractedClaim.Status.FAILED, "Failed"),
]


@staff_required
def review_queue(request):
    visible = claims_visible_to(request.user)
    base = ExtractedClaim.objects.filter(claim__in=visible)
    counts = dict(base.order_by().values_list("status").annotate(n=Count("id")))
    status = request.GET.get("status", ExtractedClaim.Status.NEEDS_REVIEW)
    if status not in dict(REVIEW_TABS):
        status = ExtractedClaim.Status.NEEDS_REVIEW
    # "high" sorts before "normal", so ascending priority puts the urgent ones first.
    items = (
        base.filter(status=status).select_related("claim", "claim__claimant").order_by("priority", "created_at")
    )
    tabs = [(value, label, counts.get(value, 0), value == status) for value, label in REVIEW_TABS]
    return render(
        request,
        "claims/review_queue.html",
        {"items": items, "tabs": tabs, "status": status, "unanalysed": visible.filter(extraction__isnull=True).count()},
    )


@staff_required
def review_detail(request, pk):
    claim = get_claim_for_user(request.user, pk)
    extraction = ExtractedClaim.objects.filter(claim=claim).select_related("reviewed_by").first()
    form = None
    if request.method == "POST":
        if extraction is None:
            raise Http404
        action = request.POST.get("action", "")
        form = ReviewForm(request.POST)
        if form.is_valid():
            try:
                apply_review(
                    extraction, request.user, action,
                    edits=form.edits() if action == "approve" else None,
                    note=form.cleaned_data["note"],
                )
            except ReviewError as exc:
                messages.error(request, str(exc))
            else:
                done = {"approve": "Extraction approved.", "reject": "Extraction rejected.", "reopen": "Re-opened for review."}
                messages.success(request, done[action])
                return redirect("claims:review_detail" if action == "reopen" else "claims:review_queue",
                                *([claim.pk] if action == "reopen" else []))
    elif extraction and extraction.status not in HUMAN_DECIDED:
        form = ReviewForm(initial=ReviewForm.initial_from(extraction.data))
    analysis = (extraction.analysis if extraction else None) or {}
    return render(
        request,
        "claims/review_detail.html",
        {
            "claim": claim,
            "extraction": extraction,
            "form": form,
            "decided": bool(extraction and extraction.status in HUMAN_DECIDED),
            "flags": (analysis.get("review") or {}).get("flags", []),
            "photos": analysis.get("photos", []),
            "confidence_parts": analysis.get("confidence_parts") or {},
            "severity_reasons": analysis.get("severity_reasons", []),
            "changes": changed_fields(extraction.original_data, extraction.data) if extraction and extraction.original_data else [],
            "documents": claim.documents.all(),
        },
    )


@staff_required
@require_POST
def review_run(request, pk):
    claim = get_claim_for_user(request.user, pk)
    existing = ExtractedClaim.objects.filter(claim=claim).first()
    if existing and existing.status in HUMAN_DECIDED:
        messages.info(request, "A reviewer already decided on this. Re-open it first to run the AI check again.")
    else:
        try:
            process_claim(claim)
            messages.success(request, "AI check finished.")
        except Exception:
            import logging

            logging.getLogger(__name__).exception("AI check failed for claim %s", claim.claim_number)
            messages.error(request, "The AI check could not run. Details are in the server log.")
    return redirect("claims:review_detail", pk=claim.pk)


@staff_required
@require_POST
def review_run_pending(request):
    todo = list(claims_visible_to(request.user).filter(extraction__isnull=True)[:10])
    done = 0
    for claim in todo:
        if maybe_run(claim):
            done += 1
    messages.success(request, f"AI check finished for {done} claim(s).")
    return redirect("claims:review_queue")


def maybe_run(claim):
    """Run the AI check regardless of AI_AUTO_PROCESS; log and skip on failure."""
    import logging

    try:
        process_claim(claim)
        return True
    except Exception:
        logging.getLogger(__name__).exception("AI check failed for claim %s", claim.claim_number)
        return False
