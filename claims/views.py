from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, Q
from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render

from accounts.forms import UserRoleForm
from accounts.models import User

from .forms import (
    ClaimForm,
    DocumentUploadForm,
    MessageForm,
    StatusUpdateForm,
)
from .models import Claim, ClaimDocument
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
