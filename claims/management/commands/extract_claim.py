import json

from django.core.management.base import BaseCommand, CommandError

from claims.ai.pipeline import process_claim
from claims.models import Claim


class Command(BaseCommand):
    help = "Step 3: read a claim's documents, extract the fields and print the JSON. Usage: extract_claim CLM-XXXXXXXX"

    def add_arguments(self, parser):
        parser.add_argument("claim_number")
        parser.add_argument("--force", action="store_true", help="Re-run OCR even if already done")

    def handle(self, *args, claim_number, force, **options):
        try:
            claim = Claim.objects.get(claim_number=claim_number)
        except Claim.DoesNotExist:
            raise CommandError(f"No claim with number {claim_number}")
        extraction = process_claim(claim, force_ocr=force)
        self.stdout.write(json.dumps(extraction.data, indent=2, ensure_ascii=False))
        self.stdout.write(f"\nStatus: {extraction.get_status_display()}")
        a = extraction.analysis
        self.stdout.write(f"Confidence parts: {a.get('confidence_parts')}")
        self.stdout.write(f"Severity because: {'; '.join(a.get('severity_reasons', []))}")
        for p in a.get("photos", []):
            self.stdout.write(f"Photo {p['name']}: {'OK' if p['usable'] else ', '.join(p['issues'])} (sharpness {p['blur_score']}, brightness {p['brightness']})")
        for issue in a.get("validation", {}).get("issues", []):
            if issue["level"] == "info":
                self.stdout.write(f"Note: {issue['message']}")
        for reason in extraction.review_reasons:
            self.stdout.write(self.style.WARNING(f"WARNING: {reason}"))
