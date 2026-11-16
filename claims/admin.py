from django.contrib import admin

from .models import Claim, ClaimDocument, ClaimMessage, ExtractedClaim, StatusHistory


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


@admin.register(ExtractedClaim)
class ExtractedClaimAdmin(admin.ModelAdmin):
    list_display = ("claim", "status", "reviewed_by", "created_at")
    list_filter = ("status",)
    search_fields = ("claim__claim_number", "claim__policy_number")
    readonly_fields = ("created_at", "updated_at")
