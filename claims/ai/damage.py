"""Step 4: damage analysis (free version).

HONEST LIMIT: without a trained vision model, software cannot reliably see dents or cracks
in a photo. So this version
  1. checks each photo is usable (sharp, well lit, big enough) and spots duplicate photos,
  2. estimates severity from the damage WORDS and the claimed AMOUNT, and
  3. calculates a confidence score from what was found.
Every severity from here is a rough guess for a human reviewer, not a verdict.
Upgrade path: a vision LLM looks at the photos (see docs/UPGRADES.md, row 3).
"""
import io
import re
from dataclasses import asdict, dataclass, field

from django.conf import settings
from PIL import Image, ImageFilter, ImageOps, ImageStat

# ------------------------------------------------------------------ photo checks
MIN_SIDE_PIXELS = 480
BLUR_SCORE_MIN = 40.0  # variance of edges; below this the photo is soft/blurry (tune with real photos)
DARK_BELOW, BRIGHT_ABOVE = 40, 225  # mean brightness 0-255
DUPLICATE_DISTANCE = 5  # of 64 bits in the picture fingerprint

_LAPLACIAN = ImageFilter.Kernel((3, 3), [0, 1, 0, 1, -4, 1, 0, 1, 0], scale=1, offset=128)


@dataclass
class PhotoQuality:
    name: str
    width: int
    height: int
    blur_score: float
    brightness: float
    usable: bool
    issues: list = field(default_factory=list)
    fingerprint: int = 0  # not shown to people; used to find duplicates


def _fingerprint(gray: Image.Image) -> int:
    """64-bit 'average hash': survives resizing and re-saving, differs for different pictures."""
    small = gray.resize((8, 8), Image.LANCZOS)
    pixels = list(small.getdata())
    mean = sum(pixels) / 64
    return sum(1 << i for i, p in enumerate(pixels) if p > mean)


def check_photo_quality(data: bytes, name: str) -> PhotoQuality:
    image = ImageOps.exif_transpose(Image.open(io.BytesIO(data)))
    gray = image.convert("L")
    width, height = gray.size
    work = gray if width <= 1024 else gray.resize((1024, int(height * 1024 / width)))  # same scale for all photos
    blur_score = round(ImageStat.Stat(work.filter(_LAPLACIAN)).var[0], 1)
    brightness = round(ImageStat.Stat(gray).mean[0], 1)

    issues = []
    if min(width, height) < MIN_SIDE_PIXELS:
        issues.append(f"low resolution ({width}x{height})")
    if blur_score < BLUR_SCORE_MIN:
        issues.append("blurry or very low detail")
    if brightness < DARK_BELOW:
        issues.append("too dark")
    elif brightness > BRIGHT_ABOVE:
        issues.append("overexposed")
    return PhotoQuality(name, width, height, blur_score, brightness, not issues, issues, _fingerprint(gray))


def find_duplicates(photos: list[PhotoQuality]) -> list[tuple[str, str]]:
    pairs = []
    for i, a in enumerate(photos):
        for b in photos[i + 1 :]:
            if bin(a.fingerprint ^ b.fingerprint).count("1") <= DUPLICATE_DISTANCE:
                pairs.append((a.name, b.name))
    return pairs


# ---------------------------------------------------------------- severity guess
RANK = {None: 0, "minor": 1, "moderate": 2, "severe": 3}
NAMES = {v: k for k, v in RANK.items()}

SEVERE_TERMS = (
    "total loss", "totalled", "totaled", "written off", "write-off", "airbag", "airbags",
    "chassis", "engine", "fire", "burnt", "burned", "rolled over", "rollover", "overturned",
    "structural", "fatal", "death", "died", "hospital", "hospitalised", "hospitalized", "injured", "injury", "injuries",
)
MODERATE_TERMS = (
    "shattered", "bent", "crumpled", "radiator", "suspension", "axle", "roof", "pillar",
    "quarter panel", "windshield", "windscreen", "bonnet", "hood", "fender", "door",
)
MINOR_TERMS = ("scratch", "scratches", "scuff", "dent", "dented", "paint", "crack", "cracked", "bumper", "mirror")
PARTS = (
    "bumper", "boot", "door", "fender", "bonnet", "hood", "windshield", "windscreen", "headlight",
    "tail light", "taillight", "mirror", "roof", "quarter panel", "radiator", "tyre", "tire", "grille",
)
_NO_INJURY = re.compile(r"\b(?:no|nobody|none|without)\b[^.]{0,40}\binjur\w*", re.IGNORECASE)


def _hits(text: str, terms) -> list[str]:
    return [t for t in terms if re.search(rf"\b{re.escape(t)}\b", text)]


def estimate_severity(damage_text: str, amount=None):
    """Returns (severity or None, reasons). Takes the highest of the text guess and the amount guess."""
    text = _NO_INJURY.sub(" ", (damage_text or "").lower())
    levels = []  # (rank, reason)

    if hits := _hits(text, SEVERE_TERMS):
        levels.append((3, f"severe wording: {', '.join(hits)}"))
    if hits := _hits(text, MODERATE_TERMS):
        levels.append((2, f"moderate wording: {', '.join(hits)}"))
    parts = _hits(text, PARTS)
    if len(parts) >= 5:
        levels.append((3, f"{len(parts)} different parts damaged"))
    elif len(parts) >= 3:
        levels.append((2, f"{len(parts)} different parts damaged"))
    if hits := _hits(text, MINOR_TERMS):
        levels.append((1, f"minor wording: {', '.join(hits)}"))

    if amount is not None:
        amount = float(amount)
        if amount >= settings.DAMAGE_AMOUNT_SEVERE:
            levels.append((3, f"claimed amount {amount:,.0f} is at or above {settings.DAMAGE_AMOUNT_SEVERE:,}"))
        elif amount >= settings.DAMAGE_AMOUNT_MODERATE:
            levels.append((2, f"claimed amount {amount:,.0f} is at or above {settings.DAMAGE_AMOUNT_MODERATE:,}"))

    if not levels:
        return None, ["no damage wording found"]
    top = max(rank for rank, _ in levels)
    return NAMES[top], [reason for rank, reason in levels if rank == top] + [
        reason for rank, reason in levels if rank != top
    ]


# ------------------------------------------------------------------- confidence
KEY_FIELDS = ("customer_name", "policy_number", "accident_description", "damage_description")


def confidence_score(data: dict, ocr_confidences: list, photos: list[PhotoQuality]):
    """confidence = completeness x OCR quality x photo quality (each 0-1). Returns (score, parts)."""
    found = sum(1 for k in KEY_FIELDS if data.get(k)) + (1 if (data.get("vehicle_details") or {}).get("make") else 0)
    completeness = found / (len(KEY_FIELDS) + 1)
    ocr = min(ocr_confidences) if ocr_confidences else 1.0
    photo = 1.0 if not photos else max(0.5, sum(p.usable for p in photos) / len(photos))
    parts = {"completeness": round(completeness, 2), "ocr": round(ocr, 2), "photos": round(photo, 2)}
    return round(completeness * ocr * photo, 2), parts


def photo_report(photo: PhotoQuality) -> dict:
    d = asdict(photo)
    d.pop("fingerprint")
    return d
