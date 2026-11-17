"""Step 2b: collect everything a claimant submitted into text the AI can read."""
from dataclasses import dataclass, field

from .ocr import OCRError, extract_text

# Below this average OCR confidence we warn that a human should check the text.
LOW_CONFIDENCE = 0.60


@dataclass
class DocumentText:
    document_id: int
    name: str
    text: str
    confidence: float | None
    method: str


@dataclass
class IngestionResult:
    claim_number: str
    typed_description: str
    documents: list[DocumentText] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def combined_text(self) -> str:
        """All text in one block, each source labelled (this is what step 3 will read)."""
        parts = [f"=== TYPED DESCRIPTION ===\n{self.typed_description}"]
        for d in self.documents:
            parts.append(f"=== DOCUMENT: {d.name} ===\n{d.text}")
        return "\n\n".join(parts)


def ingest_claim(claim, force: bool = False) -> IngestionResult:
    """Run OCR on every document of `claim` and save the text on each ClaimDocument.

    Documents already processed are reused unless force=True. One bad file never stops
    the others: its problem is recorded in `errors` and on the document.
    """
    result = IngestionResult(claim_number=claim.claim_number, typed_description=claim.description)
    for doc in claim.documents.all():
        if doc.ocr_done and not force:
            result.documents.append(
                DocumentText(doc.id, doc.original_name, doc.ocr_text, doc.ocr_confidence, doc.ocr_method)
            )
            continue
        try:
            with doc.file.open("rb") as handle:
                ocr = extract_text(handle.read(), doc.original_name)
        except OCRError as exc:
            doc.ocr_error = str(exc)
            doc.ocr_done = False
            doc.save(update_fields=["ocr_error", "ocr_done"])
            result.errors.append(str(exc))
            continue
        except OSError as exc:
            message = f"{doc.original_name}: file is missing from storage ({exc})."
            doc.ocr_error = message
            doc.save(update_fields=["ocr_error"])
            result.errors.append(message)
            continue

        doc.ocr_text, doc.ocr_confidence, doc.ocr_method = ocr.text, ocr.confidence, ocr.method
        doc.ocr_error, doc.ocr_done = "", True
        doc.save(update_fields=["ocr_text", "ocr_confidence", "ocr_method", "ocr_error", "ocr_done"])
        result.documents.append(DocumentText(doc.id, doc.original_name, ocr.text, ocr.confidence, ocr.method))

        if not ocr.text:
            result.warnings.append(f"{doc.original_name}: no text found (photo of a vehicle, or unreadable).")
        elif ocr.confidence is not None and ocr.confidence < LOW_CONFIDENCE:
            result.warnings.append(
                f"{doc.original_name}: low OCR confidence ({ocr.confidence:.0%}); text may contain mistakes."
            )
    return result
