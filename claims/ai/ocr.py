"""Step 2a: turn an uploaded file (image / PDF / text) into plain text.

Free backend: Tesseract OCR. Digital PDFs that already contain text skip OCR.
To upgrade later, add another backend function and set OCR_BACKEND (docs/UPGRADES.md).
"""
import io
from dataclasses import dataclass

from django.conf import settings

# A digital PDF page with fewer characters than this is treated as a scan.
MIN_TEXT_CHARS_PER_PAGE = 30


class OCRError(Exception):
    """Raised with a message a person can act on."""


@dataclass
class OCRResult:
    text: str
    confidence: float | None  # 0-1, None when the text was read directly (no OCR)
    method: str  # "plain_text", "pdf_text_layer", "tesseract"
    pages: int = 1


def extract_text(data: bytes, filename: str) -> OCRResult:
    """Read text from file bytes. Raises OCRError if it cannot."""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if settings.OCR_BACKEND != "tesseract":
        raise OCRError(f"Unknown OCR_BACKEND '{settings.OCR_BACKEND}'. Use 'tesseract'.")
    if ext in {"txt", "text"}:
        return OCRResult(data.decode("utf-8", errors="replace").strip(), None, "plain_text")
    if ext == "pdf":
        return _read_pdf(data, filename)
    if ext in {"jpg", "jpeg", "png"}:
        return _ocr_image_bytes(data, filename)
    raise OCRError(f"{filename}: unsupported file type '.{ext}'. Use JPG, PNG, PDF or TXT.")


# ---------------------------------------------------------------- Tesseract


def _tesseract():
    try:
        import pytesseract
    except ImportError as exc:
        raise OCRError("pytesseract is not installed. Run: pip install -r requirements.txt") from exc
    if settings.TESSERACT_CMD:
        pytesseract.pytesseract.tesseract_cmd = settings.TESSERACT_CMD
    return pytesseract


def _prepare(image):
    """Make a photo/scan easier for Tesseract: right way up, grayscale, bigger, higher contrast."""
    from PIL import ImageOps

    image = ImageOps.exif_transpose(image).convert("L")
    if image.width < 1200:  # small images OCR badly
        scale = 1200 / image.width
        image = image.resize((1200, int(image.height * scale)))
    return ImageOps.autocontrast(image)


def _ocr_image(image, label: str):
    pytesseract = _tesseract()
    image = _prepare(image)
    try:
        text = pytesseract.image_to_string(image, lang=settings.OCR_LANGUAGES)
        info = pytesseract.image_to_data(
            image, lang=settings.OCR_LANGUAGES, output_type=pytesseract.Output.DICT
        )
    except pytesseract.TesseractNotFoundError as exc:
        raise OCRError(
            "Tesseract is not installed or not on PATH. Install it, or set TESSERACT_CMD "
            "to the full path of tesseract.exe."
        ) from exc
    except pytesseract.TesseractError as exc:
        raise OCRError(f"{label}: Tesseract failed ({exc}). Is language '{settings.OCR_LANGUAGES}' installed?") from exc
    scores = [float(c) for c, w in zip(info["conf"], info["text"]) if w.strip() and float(c) >= 0]
    confidence = round(sum(scores) / len(scores) / 100, 3) if scores else 0.0
    return text.strip(), confidence


def _ocr_image_bytes(data: bytes, filename: str) -> OCRResult:
    from PIL import Image, UnidentifiedImageError

    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except (UnidentifiedImageError, OSError) as exc:
        raise OCRError(f"{filename}: not a readable image (file may be corrupt).") from exc
    text, confidence = _ocr_image(image, filename)
    return OCRResult(text, confidence, "tesseract")


# ---------------------------------------------------------------------- PDF


def _read_pdf(data: bytes, filename: str) -> OCRResult:
    from pypdf import PdfReader
    from pypdf.errors import PyPdfError

    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise OCRError(f"{filename}: PDF is password-protected.")
        page_count = len(reader.pages)
        if page_count > settings.OCR_MAX_PDF_PAGES:
            raise OCRError(
                f"{filename}: {page_count} pages is over the {settings.OCR_MAX_PDF_PAGES}-page limit."
            )
        layer = "\n".join((page.extract_text() or "") for page in reader.pages).strip()
    except PyPdfError as exc:
        raise OCRError(f"{filename}: not a readable PDF ({exc}).") from exc

    if len(layer) >= MIN_TEXT_CHARS_PER_PAGE * page_count:
        return OCRResult(layer, None, "pdf_text_layer", pages=page_count)
    return _ocr_scanned_pdf(data, filename, page_count)


def _ocr_scanned_pdf(data: bytes, filename: str, page_count: int) -> OCRResult:
    try:
        from pdf2image import convert_from_bytes
        from pdf2image.exceptions import PDFInfoNotInstalledError, PDFPageCountError
    except ImportError as exc:
        raise OCRError("pdf2image is not installed. Run: pip install -r requirements.txt") from exc
    try:
        images = convert_from_bytes(
            data, dpi=200, poppler_path=settings.POPPLER_PATH, last_page=settings.OCR_MAX_PDF_PAGES
        )
    except PDFInfoNotInstalledError as exc:
        raise OCRError(
            "Poppler is not installed (needed to read scanned PDFs). Install it, or set POPPLER_PATH "
            "to its 'bin' folder."
        ) from exc
    except PDFPageCountError as exc:
        raise OCRError(f"{filename}: could not read the PDF pages.") from exc

    texts, scores = [], []
    for number, image in enumerate(images, start=1):
        text, confidence = _ocr_image(image, f"{filename} page {number}")
        texts.append(text)
        scores.append(confidence)
    confidence = round(sum(scores) / len(scores), 3) if scores else 0.0
    return OCRResult("\n\n".join(texts).strip(), confidence, "tesseract", pages=page_count)
