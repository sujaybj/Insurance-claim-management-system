import io
import shutil
import tempfile
import unittest

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase, override_settings
from PIL import Image, ImageDraw, ImageFont

from .ai.ingestion import ingest_claim
from .ai.ocr import OCRError, extract_text
from .models import ClaimDocument
from .tests import make_claim, make_user

HAS_OCR = bool(shutil.which("tesseract"))
needs_tesseract = unittest.skipUnless(HAS_OCR, "Tesseract is not installed")
TMP_MEDIA = tempfile.mkdtemp()


def form_image(lines):
    """A white page with black text, like a (very clean) scanned form."""
    image = Image.new("RGB", (1400, 140 * len(lines) + 80), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=48)
    for i, line in enumerate(lines):
        draw.text((60, 50 + i * 120), line, fill="black", font=font)
    return image


def image_bytes(image, fmt="PNG"):
    buf = io.BytesIO()
    image.save(buf, fmt)
    return buf.getvalue()


FORM_LINES = ["Customer Name: Asha Rao", "Policy Number: POL-123456", "Vehicle: Maruti Swift 2019"]

def make_text_pdf(text):
    """Build a small valid PDF that has a real text layer (no scanning needed)."""
    stream = f"BT /F1 14 Tf 50 700 Td ({text}) Tj ET".encode()
    objects = [
        b"<</Type/Catalog/Pages 2 0 R>>",
        b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
        b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>",
        b"<</Length %d>>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>",
    ]
    out, offsets = b"%PDF-1.4\n", []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref_at = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer<</Root 1 0 R/Size %d>>\nstartxref\n%d\n%%%%EOF" % (len(objects) + 1, xref_at)
    return out


TEXT_PDF = make_text_pdf("Policy Number POL-777888 claimant Ravi Kumar rear bumper damaged in collision")


class OCRTests(SimpleTestCase):
    def test_plain_text_file(self):
        r = extract_text(b"  hello claim  ", "note.txt")
        self.assertEqual((r.text, r.method, r.confidence), ("hello claim", "plain_text", None))

    def test_unsupported_type(self):
        with self.assertRaisesMessage(OCRError, "unsupported file type"):
            extract_text(b"x", "claim.docx")

    def test_corrupt_image(self):
        with self.assertRaisesMessage(OCRError, "not a readable image"):
            extract_text(b"definitely not a png", "broken.png")

    def test_corrupt_pdf(self):
        with self.assertRaisesMessage(OCRError, "not a readable PDF"):
            extract_text(b"not a pdf at all", "broken.pdf")

    def test_pdf_with_text_layer_skips_ocr(self):
        r = extract_text(TEXT_PDF, "digital.pdf")
        self.assertEqual(r.method, "pdf_text_layer")
        self.assertIn("POL-777888", r.text)

    @needs_tesseract
    def test_image_ocr_reads_form(self):
        r = extract_text(image_bytes(form_image(FORM_LINES)), "form.png")
        self.assertEqual(r.method, "tesseract")
        self.assertIn("Asha Rao", r.text)
        self.assertIn("123456", r.text)
        self.assertGreater(r.confidence, 0.6)

    @needs_tesseract
    @unittest.skipUnless(shutil.which("pdftoppm"), "Poppler is not installed")
    def test_scanned_pdf_is_ocrd(self):
        r = extract_text(image_bytes(form_image(FORM_LINES), "PDF"), "scan.pdf")
        self.assertEqual(r.method, "tesseract")
        self.assertIn("Asha Rao", r.text)


@override_settings(MEDIA_ROOT=TMP_MEDIA)
class IngestionTests(TestCase):
    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(TMP_MEDIA, ignore_errors=True)
        super().tearDownClass()

    def add_doc(self, claim, name, content):
        return ClaimDocument.objects.create(
            claim=claim, file=SimpleUploadedFile(name, content), original_name=name
        )

    def setUp(self):
        self.claim = make_claim(make_user("ria"))

    def test_text_saved_on_document_and_reused(self):
        doc = self.add_doc(self.claim, "digital.pdf", TEXT_PDF)
        result = ingest_claim(self.claim)
        doc.refresh_from_db()
        self.assertTrue(doc.ocr_done)
        self.assertIn("POL-777888", doc.ocr_text)
        self.assertIn("=== DOCUMENT: digital.pdf ===", result.combined_text())
        self.assertIn(self.claim.description, result.combined_text())
        # second run reuses the saved text instead of reading the file again
        doc.file.delete(save=False)
        again = ingest_claim(self.claim)
        self.assertEqual(again.errors, [])
        self.assertIn("POL-777888", again.documents[0].text)

    def test_one_bad_file_does_not_stop_the_others(self):
        self.add_doc(self.claim, "broken.png", b"garbage")
        self.add_doc(self.claim, "digital.pdf", TEXT_PDF)
        result = ingest_claim(self.claim)
        self.assertEqual(len(result.documents), 1)
        self.assertEqual(len(result.errors), 1)
        bad = self.claim.documents.get(original_name="broken.png")
        self.assertFalse(bad.ocr_done)
        self.assertIn("not a readable image", bad.ocr_error)

    @needs_tesseract
    def test_photo_without_text_gives_warning(self):
        blank = Image.new("RGB", (1400, 900), (90, 120, 150))
        self.add_doc(self.claim, "car.jpg", image_bytes(blank, "JPEG"))
        result = ingest_claim(self.claim)
        self.assertTrue(any("no text found" in w for w in result.warnings))
