import uuid

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.utils import timezone


def generate_claim_number():
    return "CLM-" + uuid.uuid4().hex[:8].upper()


def document_upload_path(instance, filename):
    # Random name avoids collisions / guessing; original name kept in the DB.
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "bin"
    return f"claims/{instance.claim.claim_number}/{uuid.uuid4().hex}.{ext}"


class Claim(models.Model):
    class Status(models.TextChoices):
        RECEIVED = "received", "Received"
        UNDER_REVIEW = "under_review", "Under Review"
        APPROVED = "approved", "Approved"
        DENIED = "denied", "Denied"

    class ClaimType(models.TextChoices):
        AUTO = "auto", "Auto"
        HEALTH = "health", "Health"
        HOME = "home", "Home / Property"
        TRAVEL = "travel", "Travel"
        LIFE = "life", "Life"

    claim_number = models.CharField(
        max_length=20, unique=True, default=generate_claim_number, editable=False
    )
    claimant = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="claims"
    )
    assigned_agent = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assigned_claims",
        limit_choices_to={"role": "agent"},
    )
    policy_number = models.CharField(max_length=30)
    claim_type = models.CharField(max_length=10, choices=ClaimType.choices)
    incident_date = models.DateField()
    amount_claimed = models.DecimalField(
        max_digits=12, decimal_places=2, validators=[MinValueValidator(1)]
    )
    description = models.TextField()
    status = models.CharField(
        max_length=15, choices=Status.choices, default=Status.RECEIVED, db_index=True
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["claimant", "status"])]

    def __str__(self):
        return f"{self.claim_number} ({self.get_status_display()})"

    def change_status(self, new_status, changed_by, note=""):
        """Update status, record history. Returns the history row (or None if unchanged)."""
        if new_status not in self.Status.values:
            raise ValueError(f"Invalid status: {new_status}")
        if new_status == self.status:
            return None
        old = self.status
        self.status = new_status
        self.save(update_fields=["status", "updated_at"])
        return StatusHistory.objects.create(
            claim=self,
            old_status=old,
            new_status=new_status,
            changed_by=changed_by,
            note=note,
        )


class ClaimDocument(models.Model):
    claim = models.ForeignKey(Claim, on_delete=models.CASCADE, related_name="documents")
    file = models.FileField(upload_to=document_upload_path)
    original_name = models.CharField(max_length=255)
    uploaded_at = models.DateTimeField(default=timezone.now)

    def __str__(self):
        return self.original_name


class StatusHistory(models.Model):
    claim = models.ForeignKey(Claim, on_delete=models.CASCADE, related_name="history")
    old_status = models.CharField(max_length=15, choices=Claim.Status.choices)
    new_status = models.CharField(max_length=15, choices=Claim.Status.choices)
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True
    )
    note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]
        verbose_name_plural = "status history"


class ClaimMessage(models.Model):
    """Communication thread between staff and claimant on a claim."""

    claim = models.ForeignKey(Claim, on_delete=models.CASCADE, related_name="messages")
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    body = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]


class ExtractedClaim(models.Model):
    """AI-extracted data for a claim, waiting for (or past) human review."""

    class Status(models.TextChoices):
        PROCESSING = "processing", "Processing"
        NEEDS_REVIEW = "needs_review", "Needs review"
        VERIFIED = "verified", "Verified"
        FAILED = "failed", "Failed"

    claim = models.OneToOneField(
        Claim, on_delete=models.CASCADE, related_name="extraction"
    )
    data = models.JSONField(default=dict, blank=True)  # the validated JSON
    status = models.CharField(
        max_length=15, choices=Status.choices, default=Status.PROCESSING, db_index=True
    )
    review_reasons = models.JSONField(default=list, blank=True)  # why a human must look
    error_message = models.TextField(blank=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reviewed_extractions",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Extraction for {self.claim.claim_number} ({self.get_status_display()})"
