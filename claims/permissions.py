from functools import wraps

from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect
from django.contrib.auth.decorators import login_required

from .models import Claim


def role_required(check):
    """Decorator: user must be logged in and pass `check(user)`, else 403."""

    def decorator(view):
        @login_required
        @wraps(view)
        def wrapper(request, *args, **kwargs):
            if not check(request.user):
                raise PermissionDenied
            return view(request, *args, **kwargs)

        return wrapper

    return decorator


staff_required = role_required(lambda u: u.is_staff_role)
admin_required = role_required(lambda u: u.is_admin_role)


def claims_visible_to(user):
    """Claimants see only their own claims; agents see assigned + unassigned; admins see all."""
    qs = Claim.objects.select_related("claimant", "assigned_agent")
    if user.is_admin_role:
        return qs
    if user.is_agent:
        from django.db.models import Q

        return qs.filter(Q(assigned_agent=user) | Q(assigned_agent__isnull=True))
    return qs.filter(claimant=user)


def get_claim_for_user(user, pk):
    """Object-level access control: 404 if the user may not see the claim."""
    return get_object_or_404(claims_visible_to(user), pk=pk)
