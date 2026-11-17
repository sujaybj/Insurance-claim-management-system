from django.core.management.base import BaseCommand, CommandError

from claims.ai.ingestion import ingest_claim
from claims.models import Claim


class Command(BaseCommand):
    help = "Step 2: run OCR on a claim's documents and print the text. Usage: process_claim CLM-XXXXXXXX"

    def add_arguments(self, parser):
        parser.add_argument("claim_number")
        parser.add_argument("--force", action="store_true", help="Re-run OCR even if already done")

    def handle(self, *args, claim_number, force, **options):
        try:
            claim = Claim.objects.get(claim_number=claim_number)
        except Claim.DoesNotExist:
            raise CommandError(f"No claim with number {claim_number}")
        result = ingest_claim(claim, force=force)
        self.stdout.write(result.combined_text())
        for w in result.warnings:
            self.stdout.write(self.style.WARNING(f"WARNING: {w}"))
        for e in result.errors:
            self.stdout.write(self.style.ERROR(f"ERROR: {e}"))
        self.stdout.write(self.style.SUCCESS(
            f"Done: {len(result.documents)} document(s) read, {len(result.errors)} error(s)."
        ))
