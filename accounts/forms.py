import re

from django import forms
from django.contrib.auth.forms import UserCreationForm

from .models import User


class RegistrationForm(UserCreationForm):
    """Public registration. Always creates a claimant; staff roles are assigned by admins."""

    class Meta:
        model = User
        fields = ("username", "email", "first_name", "last_name", "phone")

    def clean_email(self):
        email = self.cleaned_data["email"].lower()
        if User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError("An account with this email already exists.")
        return email

    def clean_phone(self):
        phone = self.cleaned_data.get("phone", "").strip()
        if phone and not re.fullmatch(r"\+?[0-9\s\-]{7,15}", phone):
            raise forms.ValidationError("Enter a valid phone number.")
        return phone

    def save(self, commit=True):
        user = super().save(commit=False)
        user.role = User.Role.CLAIMANT
        if commit:
            user.save()
        return user


class UserRoleForm(forms.ModelForm):
    """Used by admins to change a user's role / active state."""

    class Meta:
        model = User
        fields = ("role", "is_active")
