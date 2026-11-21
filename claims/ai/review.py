"""Step 6: human verification.

The system never approves or denies a CLAIM. It only decides whether its own reading of the
documents (the extraction) can be trusted without a person looking at it. The rule is
deliberately cautious: ANY doubt sends the extraction to a reviewer.
"""
import copy
from dataclasses import asdict, dataclass, field

from django.utils import timezone

from claims.models import ExtractedClaim

from .validation import validate_extraction

HIGH, NORMAL = "high", "normal"
# Signs of a wrong entry or possible fraud: these go to the top of the queue.
HIGH_PRIORITY_CODES = {
    "severe_damage", "policy_mismatch", "name_mismatch", "date_mismatch", "future_date", "duplicate_photo",
}


@dataclass
class Flag:
    code: str
    message: str

    @property
    def priority(self):
        return HIGH if self.code in HIGH_PRIORITY_CODES else NORMAL


@dataclass
class ReviewDecision:
    flags: list = field(default_factory=list)

    @property
    def flagged(self):
        return bool(self.flags)

    @property
    def priority(self):
        return HIGH if any(f.priority == HIGH for f in self.flags) else NORMAL

    @property
    def status(self):
        return ExtractedClaim.Status.NEEDS_REVIEW if self.flagged else ExtractedClaim.Status.AUTO_VERIFIED

    def reasons(self):
        return [f.message for f in self.flags]

    def as_dict(self):
        return {
            "flagged": self.flagged,
            "priority": self.priority,
            "flags": [{**asdict(f), "priority": f.priority} for f in self.flags],
        }


def decide(flags) -> ReviewDecision:
    """Collect flags, dropping repeats of the same message (two checks can notice the same thing)."""
    seen, unique = set(), []
    for flag in flags:
        if flag.message not in seen:
            seen.add(flag.message)
            unique.append(flag)
    return ReviewDecision(unique)


# ------------------------------------------------------------------ reviewer actions
class ReviewError(Exception):
    """A problem with the reviewer's request, worded so it can be shown on the page."""


EDITABLE_FIELDS = (
    "customer_name", "policy_number", "accident_description", "damage_description", "damage_severity",
)
EDITABLE_VEHICLE_FIELDS = ("make", "model", "year")
HUMAN_DECIDED = (ExtractedClaim.Status.VERIFIED, ExtractedClaim.Status.REJECTED)


def _merged(data: dict, edits: dict) -> dict:
    merged = copy.deepcopy(data or {})
    vehicle = merged.setdefault("vehicle_details", {})
    for name in EDITABLE_FIELDS:
        if name in edits:
            merged[name] = edits[name]
    for name in EDITABLE_VEHICLE_FIELDS:
        if f"vehicle_{name}" in edits:
            vehicle[name] = edits[f"vehicle_{name}"]
    return merged


def apply_review(extraction: ExtractedClaim, reviewer, action: str, edits: dict | None = None, note: str = ""):
    """Record a reviewer's decision. action: "approve" | "reject" | "reopen". Raises ReviewError."""
    note = (note or "").strip()

    if action == "reopen":
        extraction.status = ExtractedClaim.Status.NEEDS_REVIEW
        extraction.reviewed_by, extraction.reviewed_at = None, None
        extraction.save()
        return extraction

    if action == "reject":
        if not note:
            raise ReviewError("Please write a reason for rejecting this extraction.")
        extraction.status = ExtractedClaim.Status.REJECTED
    elif action == "approve":
        claim = extraction.claim
        data = _merged(extraction.data, edits or {})
        report = validate_extraction(claim, data, claim.description)
        if not report.is_valid:
            raise ReviewError("Cannot approve yet: " + " ".join(i.message for i in report.errors))
        clean = report.data.to_json_dict()  # types fixed (e.g. year "2019" -> 2019)
        if changed_fields(extraction.data, clean) and not extraction.original_data:
            extraction.original_data = extraction.data  # keep what the AI said, for the audit trail
        extraction.data = clean
        analysis = dict(extraction.analysis or {})
        analysis["validation"] = report.as_dict()
        extraction.analysis = analysis
        extraction.status = ExtractedClaim.Status.VERIFIED
    else:
        raise ReviewError(f"Unknown action '{action}'.")

    extraction.reviewed_by = reviewer
    extraction.reviewed_at = timezone.now()
    extraction.review_note = note
    extraction.save()
    return extraction


FIELD_LABELS = {
    "customer_name": "Customer name", "policy_number": "Policy number",
    "accident_description": "Accident description", "damage_description": "Damage description",
    "damage_severity": "Damage severity", "vehicle_make": "Vehicle make",
    "vehicle_model": "Vehicle model", "vehicle_year": "Vehicle year",
}


def changed_fields(original: dict, current: dict):
    """[(label, ai_value, reviewer_value)] for every field a reviewer changed."""
    def flat(d):
        d = d or {}
        out = {k: d.get(k) for k in EDITABLE_FIELDS}
        out.update({f"vehicle_{k}": (d.get("vehicle_details") or {}).get(k) for k in EDITABLE_VEHICLE_FIELDS})
        return out

    before, after = flat(original), flat(current)
    return [(FIELD_LABELS[k], before[k], after[k]) for k in FIELD_LABELS if before[k] != after[k]]
