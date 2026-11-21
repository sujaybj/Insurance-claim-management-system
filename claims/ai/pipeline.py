"""Run steps 2-6 for one claim and save the result on its ExtractedClaim."""
import logging

from django.conf import settings
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
from .review import HUMAN_DECIDED, Flag, decide
from .validation import validate_extraction

logger = logging.getLogger(__name__)

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
    """Quality-check the image documents that carry (almost) no text. Returns (photos, flags)."""
    docs = {d.id: d for d in claim.documents.all()}
    photos, flags = [], []
    for item in ingestion.documents:
        if not _is_photo(item):
            continue
        try:
            with docs[item.document_id].file.open("rb") as handle:
                quality = check_photo_quality(handle.read(), item.name)
        except OSError:
            flags.append(Flag("photo_problem", f"{item.name}: photo file is missing from storage."))
            continue
        except Exception:  # an unreadable image must not stop the claim
            flags.append(Flag("photo_problem", f"{item.name}: photo could not be opened for quality checks."))
            continue
        photos.append(quality)
        if not quality.usable:
            flags.append(Flag("photo_problem", f"{item.name}: photo problem - {', '.join(quality.issues)}."))
    for first, second in find_duplicates(photos):
        flags.append(Flag("duplicate_photo", f"{first} and {second} look like the same photo."))
    return photos, flags


def process_claim(claim, force_ocr: bool = False, reprocess: bool = False) -> ExtractedClaim:
    """OCR, extract, analyse, validate, then decide whether a person must look at it.

    A decision a reviewer already made is never overwritten unless reprocess=True.
    """
    extraction, _ = ExtractedClaim.objects.get_or_create(claim=claim)
    if extraction.status in HUMAN_DECIDED and not reprocess:
        return extraction
    ingestion = ingest_claim(claim, force=force_ocr)

    form_text = "\n\n".join(d.text for d in ingestion.documents if d.text)
    image_urls = [
        reverse("claims:document", args=[d.document_id])
        for d in ingestion.documents
        if d.name.lower().endswith(IMAGE_EXTENSIONS)
    ]
    outcome = extract_fields(form_text, ingestion.typed_description, image_urls)

    # --- step 4: damage analysis (free version: photo quality + wording/amount guess)
    photos, photo_flags = _check_photos(claim, ingestion)
    if claim.claim_type == claim.ClaimType.AUTO and not photos:
        photo_flags.append(Flag("no_photos", "No usable vehicle photos were found in the uploaded documents."))
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

    # --- step 6: collect every doubt; any doubt means a person looks at it
    photo_names = {p.name for p in photos}  # OCR warnings about photos are noise: a photo has no text
    flags = [Flag(i.code, i.message) for i in report.issues if i.level in ("error", "warning")]
    flags += [Flag("ocr_problem", w) for w in ingestion.warnings if w.split(":", 1)[0] not in photo_names]
    flags += [Flag("ocr_problem", e) for e in ingestion.errors]
    flags += [Flag("extraction_uncertain", w) for w in outcome.warnings]
    flags += photo_flags
    if severity == "severe":
        flags.append(Flag("severe_damage", "Severe damage estimated: " + "; ".join(severity_reasons[:2]) + "."))
    confidence = outcome.data.confidence
    if confidence is not None and confidence < settings.REVIEW_MIN_CONFIDENCE:
        flags.append(Flag("low_confidence", f"Low confidence ({confidence:.0%}): the system is not sure about this reading."))
    decision = decide(flags)

    extraction.data = data
    extraction.analysis = {
        "photos": [photo_report(p) for p in photos],
        "severity_reasons": severity_reasons,
        "confidence_parts": parts,
        "field_sources": outcome.sources,
        "validation": report.as_dict(),
        "review": decision.as_dict(),
    }
    extraction.review_reasons = decision.reasons()
    extraction.priority = decision.priority
    extraction.error_message = "\n".join(ingestion.errors)
    extraction.reviewed_by = extraction.reviewed_at = None
    extraction.review_note = ""
    if report.has_structural_errors:
        # The JSON itself is broken: nothing to review, the pipeline has to be fixed or re-run.
        extraction.status = ExtractedClaim.Status.FAILED
        extraction.error_message = "\n".join(filter(None, [extraction.error_message] + [e.message for e in report.errors]))
    else:
        extraction.status = decision.status  # NEEDS_REVIEW if anything was flagged, else AUTO_VERIFIED
    extraction.save()
    return extraction


def maybe_auto_process(claim):
    """If AI_AUTO_PROCESS is on, run the AI check. A failure here must never break a submission."""
    if not settings.AI_AUTO_PROCESS:
        return None
    try:
        return process_claim(claim)
    except Exception:
        logger.exception("AI check failed for claim %s", claim.claim_number)
        return None
