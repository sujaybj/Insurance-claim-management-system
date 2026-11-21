"""Step 5: validate the extracted JSON and cross-check it against what the claimant typed.

Every problem becomes an Issue with a code, the field, and a plain-English message, so a
reviewer (or you, debugging) can see exactly what is wrong. The checks:
  1. JSON syntax        - the JSON text parses (also used later for AI output, see parse_json_text)
  2. Data types/ranges  - year is an integer, severity is minor/moderate/severe, confidence is 0-1 ...
  3. Required fields    - customer name and policy number are present
  4. Policy format      - same rule as the claim form (6-30 letters, digits, hyphens)
  5. Missing values     - which optional fields are null
  6. Consistency        - extracted data vs the claim record and the typed description
"""
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import date

from pydantic import ValidationError

from .damage import PARTS, _hits
from .extractor import _find_year, _split_make_model
from .schema import REQUIRED_FIELDS, ExtractedClaimData

ERROR, WARNING, INFO = "error", "warning", "info"
# Problems with the JSON itself (not with the claim). The pipeline marks these claims FAILED.
STRUCTURAL_CODES = {"json_syntax", "schema"}
POLICY_PATTERN = re.compile(r"^[A-Z0-9-]{6,30}$")
OPTIONAL_FIELDS = (
    "accident_description", "damage_description", "vehicle_details.make",
    "vehicle_details.model", "vehicle_details.year",
)


@dataclass
class Issue:
    level: str  # "error" | "warning" | "info"
    code: str
    field: str
    message: str


@dataclass
class ValidationReport:
    issues: list = field(default_factory=list)
    data: ExtractedClaimData | None = None

    def add(self, level, code, field_name, message):
        self.issues.append(Issue(level, code, field_name, message))

    @property
    def errors(self):
        return [i for i in self.issues if i.level == ERROR]

    @property
    def warnings(self):
        return [i for i in self.issues if i.level == WARNING]

    @property
    def missing(self):
        return [i.field for i in self.issues if i.code in {"missing_required", "missing_optional"}]

    @property
    def has_structural_errors(self):
        return any(i.code in STRUCTURAL_CODES for i in self.issues)

    @property
    def is_valid(self):
        return not self.errors

    def reasons(self):
        """One line per error/warning, for the human-review list."""
        return [i.message for i in self.issues if i.level in (ERROR, WARNING)]

    def as_dict(self):
        return {"valid": self.is_valid, "issues": [asdict(i) for i in self.issues]}


# ------------------------------------------------------------ 1 & 2: syntax and types
def _friendly(error) -> tuple[str, str]:
    loc = ".".join(str(p) for p in error["loc"]) or "(whole object)"
    msg = error["msg"].removeprefix("Value error, ")
    return loc, msg


def parse_json_text(raw: str, report: ValidationReport | None = None):
    """Parse JSON text into ExtractedClaimData, collecting clear errors instead of crashing.

    Tolerates the ```json fences an AI model often wraps around its answer (for version 2).
    """
    report = report or ValidationReport()
    text = (raw or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
    if not text:
        report.add(ERROR, "json_syntax", "(whole object)", "The extraction result is empty, so there is no JSON to check.")
        return report
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        report.add(ERROR, "json_syntax", "(whole object)",
                   f"Invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}.")
        return report
    if not isinstance(payload, dict):
        report.add(ERROR, "json_syntax", "(whole object)",
                   f"Expected a JSON object {{...}} but got {type(payload).__name__}.")
        return report
    try:
        report.data = ExtractedClaimData.model_validate(payload)
    except ValidationError as exc:
        for err in exc.errors():
            loc, msg = _friendly(err)
            report.add(ERROR, "schema", loc, f"Field '{loc}' is invalid: {msg}.")
    return report


# ---------------------------------------------------------------- 3, 4, 5: fields
def _check_fields(report: ValidationReport):
    data = report.data
    for name in REQUIRED_FIELDS:
        if not getattr(data, name):
            label = name.replace("_", " ").capitalize()
            report.add(ERROR, "missing_required", name, f"{label} was not found in the documents or description.")
    if data.policy_number and not POLICY_PATTERN.match(data.policy_number):
        report.add(ERROR, "policy_format", "policy_number",
                   f"Policy number '{data.policy_number}' must be 6-30 letters, digits or hyphens.")
    flat = {**data.model_dump(), **{f"vehicle_details.{k}": v for k, v in data.vehicle_details.model_dump().items()}}
    for name in OPTIONAL_FIELDS:
        if not flat.get(name):
            report.add(INFO, "missing_optional", name, f"'{name}' is missing and stored as null.")


# --------------------------------------------------------------- 6: consistency
def _name_tokens(text: str) -> set[str]:
    words = re.findall(r"[a-z]+", (text or "").lower())
    return {w for w in words if len(w) > 2 and w not in {"mr", "mrs", "ms", "shri", "smt", "the"}}


def _same_make(a: str, b: str) -> bool:
    a, b = a.lower(), b.lower()
    return a == b or a.startswith(b) or b.startswith(a)  # "Maruti" == "Maruti Suzuki"


def _check_consistency(report: ValidationReport, claim, typed: str, form_date, form_date_raw):
    data, vehicle = report.data, report.data.vehicle_details

    # policy number: documents vs what the claimant entered on the claim form
    if data.policy_number and claim.policy_number and data.policy_number.upper() != claim.policy_number.upper():
        report.add(WARNING, "policy_mismatch", "policy_number",
                   f"Policy number mismatch: documents say {data.policy_number}, the claim form says {claim.policy_number}.")

    # customer name: documents vs the claimant's account (only if the account has a real name)
    account_name = claim.claimant.get_full_name().strip()
    if data.customer_name and account_name:
        if not (_name_tokens(data.customer_name) & _name_tokens(account_name)):
            report.add(WARNING, "name_mismatch", "customer_name",
                       f"Name mismatch: documents say '{data.customer_name}', the account holder is '{account_name}'.")

    # accident date: written on the form vs the date entered on the claim
    if form_date_raw and not form_date:
        report.add(WARNING, "invalid_date", "incident_date",
                   f"The accident date '{form_date_raw}' on the form is not a valid date (expected day/month/year).")
    if form_date:
        if form_date > date.today():
            report.add(WARNING, "future_date", "incident_date", f"The accident date {form_date:%d %b %Y} on the form is in the future.")
        if claim.incident_date and form_date != claim.incident_date:
            report.add(WARNING, "date_mismatch", "incident_date",
                       f"Date mismatch: the form says {form_date:%d %b %Y}, the claim says {claim.incident_date:%d %b %Y}.")

    # vehicle: documents vs what the claimant typed in the description
    typed_make, typed_model = _split_make_model(typed or "")
    typed_year = _find_year(typed or "")
    if typed_make and vehicle.make and not _same_make(typed_make, vehicle.make):
        report.add(WARNING, "vehicle_mismatch", "vehicle_details.make",
                   f"Vehicle mismatch: the description mentions a {typed_make}, the documents say {vehicle.make}.")
    elif typed_model and vehicle.model and not (_name_tokens(typed_model) & _name_tokens(vehicle.model)):
        report.add(WARNING, "vehicle_mismatch", "vehicle_details.model",
                   f"Vehicle mismatch: the description mentions model '{typed_model}', the documents say '{vehicle.model}'.")
    if typed_year and vehicle.year and typed_year != vehicle.year:
        report.add(WARNING, "vehicle_mismatch", "vehicle_details.year",
                   f"Vehicle year mismatch: the description says {typed_year}, the documents say {vehicle.year}.")

    # damaged parts: typed description vs the damage written on the form
    typed_parts, form_parts = set(_hits((typed or "").lower(), PARTS)), set(_hits((data.damage_description or "").lower(), PARTS))
    if typed_parts and form_parts and not (typed_parts & form_parts):
        report.add(WARNING, "damage_mismatch", "damage_description",
                   f"Damage mismatch: the description mentions {', '.join(sorted(typed_parts))}, "
                   f"the form lists {', '.join(sorted(form_parts))}.")


# ------------------------------------------------------------------ entry point
def validate_extraction(claim, data: dict, typed_description: str = "", form_date=None, form_date_raw=None):
    """Run every check on the extracted dict. Never raises; returns a ValidationReport."""
    try:
        text = json.dumps(data, ensure_ascii=False)  # the JSON exactly as it will be stored
    except (TypeError, ValueError) as exc:
        report = ValidationReport()
        report.add(ERROR, "json_syntax", "(whole object)", f"The extracted data cannot be written as JSON: {exc}.")
        return report
    report = parse_json_text(text)
    if report.data is None:
        return report
    _check_fields(report)
    _check_consistency(report, claim, typed_description, form_date, form_date_raw)
    return report
