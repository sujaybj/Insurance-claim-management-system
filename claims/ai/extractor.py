"""Step 3: turn claim text into the structured JSON (free, rule-based version).

Reads labelled lines such as "Policy Number: POL-123456" from the OCR text, and falls
back to the claimant's typed description. Anything it cannot find stays null.
Upgrade path: replace `extract_fields` with an LLM call returning the same
ExtractedClaimData (see docs/UPGRADES.md).
"""
import re
from dataclasses import dataclass, field
from datetime import date

from django.urls import reverse

from .schema import ExtractedClaimData

# Vehicle makes common on Indian roads. Longest names first so "Maruti Suzuki" beats "Suzuki".
VEHICLE_MAKES = sorted(
    [
        "Maruti Suzuki", "Maruti", "Hyundai", "Tata", "Mahindra", "Honda", "Toyota", "Kia",
        "Renault", "Ford", "Volkswagen", "Skoda", "MG", "Nissan", "Datsun", "Chevrolet",
        "Fiat", "Jeep", "Citroen", "Mitsubishi", "Volvo", "BMW", "Mercedes-Benz", "Mercedes",
        "Audi", "Lexus", "Isuzu", "Force", "Suzuki", "Bajaj", "Hero", "TVS", "Royal Enfield",
        "Yamaha", "KTM", "Ashok Leyland", "Eicher",
    ],
    key=len,
    reverse=True,
)

DAMAGE_WORDS = (
    "dent", "crack", "scratch", "broken", "smash", "shatter", "bumper", "windshield",
    "windscreen", "headlight", "tail light", "taillight", "bonnet", "hood", "boot", "door",
    "fender", "mirror", "bent", "crumpled", "damage", "flat tyre", "puncture",
)

LABELS = {
    "name": r"(?:customer|claimant|insured|policy\s*holder|policyholder|owner)(?:'s)?\s*name|name\s+of\s+(?:the\s+)?(?:insured|policy\s*holder|claimant|customer)|insured|policy\s*holder",
    "policy": r"policy\s*(?:no\.?|number|num|#)|policy\s*id",
    "accident": r"(?:description\s+of\s+)?(?:accident|incident)(?:\s+description|\s+details)?|how\s+did\s+the\s+accident\s+happen|accident\s+details|details\s+of\s+(?:the\s+)?(?:accident|incident)",
    "damage": r"(?:description\s+of\s+)?damage(?:s)?(?:\s+description|\s+details)?|nature\s+of\s+damage|damage\s+details",
    "vehicle": r"vehicle(?:\s+details)?|car|make\s*(?:&|and|/)\s*model|make/model",
    "make": r"(?:vehicle\s+)?make|manufacturer",
    "model": r"(?:vehicle\s+)?model(?!\s*year)",
    "year": r"(?:vehicle\s+)?year(?:\s+of\s+(?:manufacture|make|registration))?|model\s*year|mfg\.?\s*year|manufacturing\s+year",
}

# Any "Label:" at the start of a line or after a gap - used to cut one value off from the next field.
_ANY_LABEL = re.compile(r"(?:\s{2,}|\s\|\s)[A-Z][A-Za-z/&.' ]{2,30}\s*[:;]\s")
_KNOWN_LABEL = re.compile(r"\s(?:" + "|".join(LABELS.values()) + r")\s*[:;]", re.IGNORECASE)


def _split_columns(text: str) -> str:
    """Put side-by-side fields ("NAME : X    POLICY NO. : Y") on separate lines."""
    return re.sub(r"[ \t]+(?=(?:" + "|".join(LABELS.values()) + r")\s*[:;])", "\n", text, flags=re.IGNORECASE)


def _tidy_case(value):
    """HYUNDAI -> Hyundai, but leave short names like MG, BMW, TVS alone."""
    if value and value.isupper() and len(value) > 3:
        return value.title()
    return value


@dataclass
class ExtractionOutcome:
    data: ExtractedClaimData
    sources: dict = field(default_factory=dict)  # field name -> "form" | "typed" | "not found"
    warnings: list = field(default_factory=list)


def _labelled_values(text: str, label_regex: str, multiline: bool = False) -> list[str]:
    """Every value that follows a matching 'Label:' in the text."""
    found = []
    lines = text.splitlines()
    pattern = re.compile(rf"^[ \t>*\-•]*(?:{label_regex})\s*[:;\-–]\s*(.*)$", re.IGNORECASE)
    for i, line in enumerate(lines):
        m = pattern.match(line)
        if not m:
            continue
        value = m.group(1)
        cuts = [m.start() for m in (_ANY_LABEL.search(value), _KNOWN_LABEL.search(value)) if m]
        if cuts:
            value = value[: min(cuts)]
        value = re.split(r"\s{3,}", value.strip())[0]
        if multiline:  # keep following lines until a blank line or the next label
            parts = [value]
            for nxt in lines[i + 1 : i + 4]:
                if not nxt.strip() or re.match(r"^\s*[A-Za-z/&.' ]{3,30}\s*[:;]", nxt):
                    break
                parts.append(nxt.strip())
            value = " ".join(p for p in parts if p)
        value = value.strip(" \t,;:-")
        if value:
            found.append(value)
    return found


def _first(text: str, key: str, multiline: bool = False):
    values = _labelled_values(text, LABELS[key], multiline)
    return values[0] if values else None


def _clean_name(raw):
    if not raw:
        return None, None
    name = re.sub(r"\s+", " ", raw).strip(" .,:;")
    if re.search(r"\d", name) or len(name) > 60 or len(name) < 2:
        return None, f"Customer name '{raw[:40]}' does not look like a name."
    if name.isupper() or name.islower():
        name = name.title()
    return name, None


def _clean_policy(raw, text=""):
    """Returns (policy_number, how) where how is "form", "cleaned" (labelled but changed) or "pattern"."""
    attempts = [(raw, "form")] if raw else []
    attempts += [(c, "pattern") for c in re.findall(r"\b[A-Za-z]{2,5}[-/]?\d{5,}\b", text)]
    for candidate, how in attempts:
        token = re.sub(r"[^A-Za-z0-9\-]", "", candidate.replace("/", "-").replace(" ", "")).upper()
        if 6 <= len(token) <= 30 and re.search(r"\d", token):
            if how == "form" and token != candidate.strip().upper():
                how = "cleaned"
            return token, how
    return None, None


def _find_year(text: str):
    this_year = date.today().year
    for m in re.finditer(r"\b(19[5-9]\d|20\d\d)\b", text or ""):
        year = int(m.group(1))
        if 1950 <= year <= this_year + 1:
            return year
    return None


def _split_make_model(text: str):
    """'Maruti Swift 2019' -> ('Maruti', 'Swift'). Model = up to two words after the make."""
    for make in VEHICLE_MAKES:
        m = re.search(rf"\b{re.escape(make)}\b[\s,\-]*(.*)", text, re.IGNORECASE)
        if m:
            rest = re.sub(r"\b\d{4}\b.*", "", m.group(1))  # drop any 4-digit year and anything after
            rest = re.sub(r"\b(year|model|reg\w*|no)\b.*", "", rest, flags=re.IGNORECASE)
            words = re.findall(r"[A-Za-z0-9][A-Za-z0-9+\-]*", rest)[:2]
            return make, (" ".join(words) or None)
    return None, None


def _damage_sentences(text: str):
    parts = re.split(r"(?<=[.!?])\s+|\n+", text or "")
    hits = [p.strip() for p in parts if any(w in p.lower() for w in DAMAGE_WORDS)]
    return " ".join(hits) or None


def extract_fields(form_text: str, typed_description: str = "", image_urls=None) -> ExtractionOutcome:
    """Build ExtractedClaimData from form/OCR text plus the claimant's typed description."""
    warnings, sources, raw = [], {}, {}
    form_text = _split_columns(form_text)
    both = f"{form_text}\n{typed_description or ''}"

    # --- customer name
    name, problem = _clean_name(_first(form_text, "name"))
    if problem:
        warnings.append(problem)
    if not name:
        name, problem = _clean_name(_first(typed_description or "", "name"))
        sources["customer_name"] = "typed" if name else "not found"
    else:
        sources["customer_name"] = "form"
    raw["customer_name"] = name

    # --- policy number
    raw_policy = _first(form_text, "policy")
    policy, how = _clean_policy(raw_policy, both)
    sources["policy_number"] = {"form": "form", "cleaned": "form", "pattern": "pattern match"}.get(how, "not found")
    if how == "pattern":
        warnings.append(f"Policy number {policy} was found by pattern, not from a 'Policy Number' label; please check it.")
    elif how == "cleaned":
        warnings.append(f"Policy number was read as '{raw_policy}' and cleaned up to {policy}; please check it.")
    raw["policy_number"] = policy

    # --- vehicle
    make = _first(form_text, "make")
    model = _first(form_text, "model")
    year_text = _first(form_text, "year") or ""
    year = _find_year(year_text)
    if year_text and not year and re.search(r"\d{4}", year_text):
        warnings.append(f"Vehicle year '{year_text[:20]}' is not a realistic year.")
    vehicle_line = _first(form_text, "vehicle") or ""
    if not make and vehicle_line:
        make, guessed_model = _split_make_model(vehicle_line)
        model = model or guessed_model
    if not make:  # last resort: look for a known make anywhere in the text
        make, guessed_model = _split_make_model(both)
        model = model or guessed_model
    if make and not model:
        _, model = _split_make_model(make + " " + (vehicle_line.split(make, 1)[-1] if make in vehicle_line else ""))
    year = year or _find_year(vehicle_line) or None
    make, model = _tidy_case(make), _tidy_case(model)
    sources["vehicle_details"] = "form" if make or model or year else "not found"
    raw["vehicle_details"] = {"make": make, "model": model, "year": year}

    # --- accident description (form first, then what the claimant typed)
    accident = _first(form_text, "accident", multiline=True)
    sources["accident_description"] = "form" if accident else "typed" if typed_description else "not found"
    accident = accident or (typed_description.strip() if typed_description else None)
    raw["accident_description"] = accident

    # --- damage description
    damage = _first(form_text, "damage", multiline=True)
    if damage:
        sources["damage_description"] = "form"
    else:
        damage = _damage_sentences(typed_description) or _damage_sentences(accident or "")
        sources["damage_description"] = "typed" if damage else "not found"
    raw["damage_description"] = damage

    raw["images"] = list(image_urls or [])
    data = ExtractedClaimData.model_validate(raw)

    for label, key in (("Customer name", "customer_name"), ("Policy number", "policy_number")):
        already_explained = any(w.lower().startswith(label.lower()) for w in warnings)
        if sources[key] == "not found" and not already_explained:
            warnings.append(f"{label} was not found in the documents or description.")
    return ExtractionOutcome(data=data, sources=sources, warnings=warnings)
