import os
import re
from datetime import date

from django import forms
from django.conf import settings

from .models import Claim, ClaimMessage


class MultipleFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class MultipleFileField(forms.FileField):
    """File field that accepts several files and validates each one."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("widget", MultipleFileInput(attrs={"accept": ".jpg,.jpeg,.png,.pdf"}))
        super().__init__(*args, **kwargs)

    def clean(self, data, initial=None):
        single = super().clean
        if isinstance(data, (list, tuple)):
            return [single(d, initial) for d in data if d]
        return [single(data, initial)] if data else []


def validate_upload(f):
    ext = os.path.splitext(f.name)[1].lower()
    if ext not in settings.ALLOWED_UPLOAD_EXTENSIONS:
        allowed = ", ".join(sorted(settings.ALLOWED_UPLOAD_EXTENSIONS))
        raise forms.ValidationError(f"'{f.name}': unsupported file type. Allowed: {allowed}")
    if f.size > settings.MAX_UPLOAD_SIZE:
        mb = settings.MAX_UPLOAD_SIZE // (1024 * 1024)
        raise forms.ValidationError(f"'{f.name}' is larger than {mb} MB.")


class ClaimForm(forms.ModelForm):
    documents = MultipleFileField(
        required=False,
        help_text="Photos, receipts or PDFs (JPG, PNG, PDF; max 5 MB each).",
    )

    class Meta:
        model = Claim
        fields = [
            "policy_number",
            "claim_type",
            "incident_date",
            "amount_claimed",
            "description",
        ]
        widgets = {
            "incident_date": forms.DateInput(attrs={"type": "date"}),
            "description": forms.Textarea(attrs={"rows": 5}),
        }

    def clean_policy_number(self):
        value = self.cleaned_data["policy_number"].strip().upper()
        if not re.fullmatch(r"[A-Z0-9\-]{6,30}", value):
            raise forms.ValidationError(
                "Policy number must be 6-30 characters: letters, digits or hyphens."
            )
        return value

    def clean_incident_date(self):
        d = self.cleaned_data["incident_date"]
        if d > date.today():
            raise forms.ValidationError("Incident date cannot be in the future.")
        return d

    def clean_description(self):
        text = self.cleaned_data["description"].strip()
        if len(text) < 20:
            raise forms.ValidationError("Please describe the incident in at least 20 characters.")
        return text

    def clean_documents(self):
        files = self.cleaned_data.get("documents") or []
        if len(files) > 10:
            raise forms.ValidationError("You can upload at most 10 files per submission.")
        for f in files:
            validate_upload(f)
        return files


class StatusUpdateForm(forms.Form):
    status = forms.ChoiceField(choices=Claim.Status.choices)
    note = forms.CharField(
        required=False, widget=forms.Textarea(attrs={"rows": 2}), label="Note to claimant"
    )
    assigned_agent = forms.ModelChoiceField(
        queryset=None, required=False, empty_label="— Unassigned —"
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from accounts.models import User

        self.fields["assigned_agent"].queryset = User.objects.filter(
            role=User.Role.AGENT, is_active=True
        )


class MessageForm(forms.ModelForm):
    class Meta:
        model = ClaimMessage
        fields = ["body"]
        widgets = {"body": forms.Textarea(attrs={"rows": 3, "placeholder": "Write a message…"})}
        labels = {"body": ""}


class DocumentUploadForm(forms.Form):
    documents = MultipleFileField(required=True)

    def clean_documents(self):
        files = self.cleaned_data["documents"]
        for f in files:
            validate_upload(f)
        return files
