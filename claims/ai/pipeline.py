"""Run steps 2-5 for one claim and save the result on its ExtractedClaim."""
from django.urls import reverse

from claims.models import ExtractedClaim

from .damage import (
    check_photo_quality,
    confidence_score,
    estimate_severity,
    find_duplicates,
    photo_report,
)
from .extractor import extract_fields, find_form_date
from .ingestion import ingest_claim
from .validation import validate_extraction

IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png")
PHOTO_MAX_TEXT_CHARS = 40  # an image with more text than this is treated as a scanned form...
PHOTO_MAX_OCR_CONFIDENCE = 0.5  # ...unless Tesseract was unsure of it (stray text on plates/signs = a photo)
PHOTO_JUNK_MAX_CHARS = 200  # a long, low-confidence read is a bad scan of a form, not a photo


def _is_photo(item) -> bool:
    if not item.name.lower().endswith(IMAGE_EXTENSIONS):
        return False
    little_text = len(item.text.strip()) <= PHOTO_MAX_TEXT_CHARS
    junk_text = (
        item.confidence is not None
        and item.confidence < PHOTO_MAX_OCR_CONFIDENCE
        and len(item.text.strip()) <= PHOTO_JUNK_MAX_CHARS
    )
    return little_text or junk_text


def _check_photos(claim, ingestion):
    """Quality-check the image documents that carry (almost) no text. Returns (photos, warnings)."""
    docs = {d.id: d for d in claim.documents.all()}
    photos, warnings = [], []
    for item in ingestion.documents:
        if not _is_photo(item):
            continue
        try:
            with docs[item.document_id].file.open("rb") as handle:
                quality = check_photo_quality(handle.read(), item.name)
        except OSError:
            warnings.append(f"{item.name}: photo file is missing from storage.")
            continue
        except Exception:  # an unreadable image must not stop the claim
            warnings.append(f"{item.name}: photo could not be opened for quality checks.")
            continue
        photos.append(quality)
        if not quality.usable:
            warnings.append(f"{item.name}: photo problem - {', '.join(quality.issues)}.")
    for first, second in find_duplicates(photos):
        warnings.append(f"{first} and {second} look like the same photo.")
    return photos, warnings


def process_claim(claim, force_ocr: bool = False) -> ExtractedClaim:
    """OCR, extract, analyse damage, save. Step 5 adds validation, step 6 the review rules."""
    extraction, _ = ExtractedClaim.objects.get_or_create(claim=claim)
    ingestion = ingest_claim(claim, force=force_ocr)

    form_text = "\n\n".join(d.text for d in ingestion.documents if d.text)
    image_urls = [
        reverse("claims:document", args=[d.document_id])
        for d in ingestion.documents
        if d.name.lower().endswith(IMAGE_EXTENSIONS)
    ]
    outcome = extract_fields(form_text, ingestion.typed_description, image_urls)

    # --- step 4: damage analysis (free version: photo quality + wording/amount guess)
    photos, photo_warnings = _check_photos(claim, ingestion)
    if claim.claim_type == claim.ClaimType.AUTO and not photos:
        photo_warnings.append("No usable vehicle photos were found in the uploaded documents.")
    severity_text = " ".join(filter(None, [outcome.data.damage_description, outcome.data.accident_description]))
    severity, severity_reasons = estimate_severity(severity_text, claim.amount_claimed)
    outcome.data.damage_severity = severity

    data = outcome.data.to_json_dict()
    # OCR quality only counts for forms; a photo has no text to read, so its 0% would be unfair.
    ocr_scores = [d.confidence for d in ingestion.documents if d.confidence is not None and not _is_photo(d)]
    outcome.data.confidence, parts = confidence_score(data, ocr_scores, photos)
    data = outcome.data.to_json_dict()

    # --- step 5: validate the JSON and cross-check it against the claim and the typed description
    form_date, form_date_raw = find_form_date(form_text)
    report = validate_extraction(claim, data, ingestion.typed_description, form_date, form_date_raw)

    # A photo is not supposed to contain text, so OCR warnings about photos are just noise.
    photo_names = {p.name for p in photos}
    ocr_warnings = [w for w in ingestion.warnings if w.split(":", 1)[0] not in photo_names]
    reasons = ocr_warnings + ingestion.errors + outcome.warnings + photo_warnings + report.reasons()
    if severity == "severe":
        reasons.append("Severe damage estimated: " + "; ".join(severity_reasons[:2]) + ".")

    extraction.data = data
    extraction.analysis = {
        "photos": [photo_report(p) for p in photos],
        "severity_reasons": severity_reasons,
        "confidence_parts": parts,
        "field_sources": outcome.sources,
        "validation": report.as_dict(),
    }
    extraction.review_reasons = list(dict.fromkeys(reasons))  # same message from two checks shows once
    extraction.error_message = "\n".join(ingestion.errors)
    if report.has_structural_errors:
        # The JSON itself is broken: nothing to review, the pipeline has to be fixed or re-run.
        extraction.status = ExtractedClaim.Status.FAILED
        extraction.error_message = "\n".join(filter(None, [extraction.error_message] + [e.message for e in report.errors]))
    elif report.errors:
        # Missing essentials or a bad policy format always need a person; the rest is step 6.
        extraction.status = ExtractedClaim.Status.NEEDS_REVIEW
    else:
        extraction.status = ExtractedClaim.Status.PROCESSING
    extraction.save()
    return extraction
