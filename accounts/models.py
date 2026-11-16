from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    """Custom user with role-based access (claimant / agent / admin)."""

    class Role(models.TextChoices):
        CLAIMANT = "claimant", "Claimant"
        AGENT = "agent", "Insurance Agent"
        ADMIN = "admin", "Admin"

    email = models.EmailField("email address", unique=True)
    role = models.CharField(max_length=10, choices=Role.choices, default=Role.CLAIMANT)
    phone = models.CharField(max_length=20, blank=True)

    @property
    def is_claimant(self):
        return self.role == self.Role.CLAIMANT

    @property
    def is_agent(self):
        return self.role == self.Role.AGENT

    @property
    def is_admin_role(self):
        return self.role == self.Role.ADMIN or self.is_superuser

    @property
    def is_staff_role(self):
        """Agents and admins can work on claims."""
        return self.is_agent or self.is_admin_role

    def __str__(self):
        return f"{self.username} ({self.get_role_display()})"
