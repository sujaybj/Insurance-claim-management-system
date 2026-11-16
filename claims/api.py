"""Small REST API (session-authenticated) used for live status polling and integrations."""
from rest_framework import serializers, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from .models import Claim
from .permissions import claims_visible_to


class ClaimSerializer(serializers.ModelSerializer):
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    claimant = serializers.CharField(source="claimant.username", read_only=True)

    class Meta:
        model = Claim
        fields = [
            "id", "claim_number", "claimant", "policy_number", "claim_type",
            "incident_date", "amount_claimed", "description",
            "status", "status_display", "created_at", "updated_at",
        ]
        read_only_fields = ["status", "created_at", "updated_at"]


class ClaimViewSet(viewsets.ReadOnlyModelViewSet):
    """GET /api/claims/ (filter: ?status=&from=&to=), GET /api/claims/<id>/, GET /api/claims/<id>/status/"""

    serializer_class = ClaimSerializer

    def get_queryset(self):
        qs = claims_visible_to(self.request.user)
        p = self.request.query_params
        if p.get("status") in Claim.Status.values:
            qs = qs.filter(status=p["status"])
        if p.get("from"):
            qs = qs.filter(incident_date__gte=p["from"])
        if p.get("to"):
            qs = qs.filter(incident_date__lte=p["to"])
        return qs

    @action(detail=True, methods=["get"])
    def status(self, request, pk=None):
        claim = self.get_object()
        return Response(
            {
                "claim_number": claim.claim_number,
                "status": claim.status,
                "status_display": claim.get_status_display(),
                "updated_at": claim.updated_at,
                "history": [
                    {
                        "from": h.old_status,
                        "to": h.new_status,
                        "note": h.note,
                        "at": h.created_at,
                    }
                    for h in claim.history.all()
                ],
            }
        )
