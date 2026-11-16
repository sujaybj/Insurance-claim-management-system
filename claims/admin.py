from django.contrib import admin

from .models import Claim, ClaimDocument, ClaimMessage, StatusHistory


class DocumentInline(admin.TabularInline):
    model = ClaimDocument
    extra = 0


@admin.register(Claim)
class ClaimAdmin(admin.ModelAdmin):
    list_display = ("claim_number", "claimant", "claim_type", "status", "amount_claimed", "created_at")
    list_filter = ("status", "claim_type")
    search_fields = ("claim_number", "policy_number", "claimant__username")
    inlines = [DocumentInline]


admin.site.register(StatusHistory)
admin.site.register(ClaimMessage)
