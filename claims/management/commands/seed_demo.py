"""Create demo users and sample claims for local testing / UAT. Not for production."""
import random
from datetime import date, timedelta

from django.core.management.base import BaseCommand

from accounts.models import User
from claims.models import Claim


class Command(BaseCommand):
    help = "Create demo admin, agent, claimant accounts (password: Demo@12345) and sample claims."

    def handle(self, *args, **opts):
        pw = "Demo@12345"
        specs = [("admin_demo", "admin"), ("agent_demo", "agent"), ("claimant_demo", "claimant")]
        users = {}
        for name, role in specs:
            u, created = User.objects.get_or_create(
                username=name, defaults={"email": f"{name}@example.com", "role": role}
            )
            if created:
                u.set_password(pw)
                u.save()
            users[role] = u
        if not Claim.objects.filter(claimant=users["claimant"]).exists():
            types = list(Claim.ClaimType.values)
            statuses = list(Claim.Status.values)
            for i in range(8):
                Claim.objects.create(
                    claimant=users["claimant"],
                    policy_number=f"POL-{100000 + i}",
                    claim_type=random.choice(types),
                    incident_date=date.today() - timedelta(days=random.randint(1, 120)),
                    amount_claimed=random.randint(1000, 90000),
                    description="Sample claim generated for demonstration purposes.",
                    status=random.choice(statuses),
                )
        self.stdout.write(self.style.SUCCESS(f"Demo users ready (password {pw})."))
