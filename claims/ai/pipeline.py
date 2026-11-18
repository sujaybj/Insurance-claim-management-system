"""Run steps 2 and 3 for one claim and save the result on its ExtractedClaim."""
from django.urls import reverse

from claims.models import ExtractedClaim

from .extractor import extract_fields
from .ingestion import ingest_claim

IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png")


def process_claim(claim, force_ocr: bool = False) -> ExtractedClaim:
    """OCR the documents, extract the fields, save. Later steps add validation and review rules."""
    extraction, _ = ExtractedClaim.objects.get_or_create(claim=claim)
    ingestion = ingest_claim(claim, force=force_ocr)

    form_text = "\n\n".join(d.text for d in ingestion.documents if d.text)
    image_urls = [
        reverse("claims:document", args=[d.document_id])
        for d in ingestion.documents
        if d.name.lower().endswith(IMAGE_EXTENSIONS)
    ]
    outcome = extract_fields(form_text, ingestion.typed_description, image_urls)

    reasons = ingestion.warnings + ingestion.errors + outcome.warnings
    extraction.data = outcome.data.to_json_dict()
    extraction.review_reasons = reasons
    extraction.error_message = "\n".join(ingestion.errors)
    # Missing essentials always need a person; the rest is decided by steps 5 and 6.
    if outcome.data.missing_required():
        extraction.status = ExtractedClaim.Status.NEEDS_REVIEW
    else:
        extraction.status = ExtractedClaim.Status.PROCESSING
    extraction.save()
    return extraction
