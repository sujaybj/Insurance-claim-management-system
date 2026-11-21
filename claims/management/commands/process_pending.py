from django.core.management.base import BaseCommand

from claims.ai.pipeline import process_claim
from claims.models import Claim


class Command(BaseCommand):
    help = "Run the AI check on every claim that has not been analysed yet."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=50)

    def handle(self, *args, limit, **options):
        todo = Claim.objects.filter(extraction__isnull=True)[:limit]
        done = 0
        for claim in todo:
            ex = process_claim(claim)
            done += 1
            self.stdout.write(f"{claim.claim_number}: {ex.get_status_display()} ({ex.get_priority_display()} priority)")
        self.stdout.write(self.style.SUCCESS(f"Analysed {done} claim(s)."))
