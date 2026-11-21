import io
import random
import shutil
import tempfile

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase, override_settings
from PIL import Image, ImageDraw, ImageFilter

from .ai.damage import (
    check_photo_quality,
    confidence_score,
    estimate_severity,
    find_duplicates,
)
from .ai.pipeline import process_claim
from .models import ClaimDocument
from .tests import make_claim, make_user
from .tests_ingestion import FORM_LINES, form_image, image_bytes, needs_tesseract


def scene(seed=1, w=1280, h=960):
    """A busy picture standing in for a sharp photo."""
    r = random.Random(seed)
    im = Image.new("RGB", (w, h), (120, 140, 160))
    d = ImageDraw.Draw(im)
    for _ in range(60):
        x, y, s = r.randint(0, w), r.randint(0, h), r.randint(30, 260)
        d.rectangle([x, y, x + s, y + s // 2], fill=tuple(r.randint(20, 230) for _ in range(3)), outline=(0, 0, 0), width=3)
    for _ in range(25):
        d.line([r.randint(0, w), r.randint(0, h), r.randint(0, w), r.randint(0, h)], fill=(20, 20, 20), width=2)
    return im


def jpeg(im, quality=90):
    return image_bytes(im, "JPEG") if quality == 90 else _save(im, quality)


def _save(im, quality):
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=quality)
    return buf.getvalue()


class PhotoQualityTests(SimpleTestCase):
    def test_sharp_photo_is_usable(self):
        q = check_photo_quality(jpeg(scene()), "ok.jpg")
        self.assertTrue(q.usable, q.issues)

    def test_blurry_photo_flagged(self):
        q = check_photo_quality(jpeg(scene().filter(ImageFilter.GaussianBlur(6))), "blur.jpg")
        self.assertFalse(q.usable)
        self.assertIn("blurry or very low detail", q.issues)

    def test_dark_photo_flagged(self):
        q = check_photo_quality(jpeg(Image.new("RGB", (1000, 800), (8, 8, 8))), "dark.jpg")
        self.assertIn("too dark", q.issues)

    def test_overexposed_photo_flagged(self):
        q = check_photo_quality(jpeg(Image.new("RGB", (1000, 800), (250, 250, 250))), "white.jpg")
        self.assertIn("overexposed", q.issues)

    def test_small_photo_flagged(self):
        q = check_photo_quality(jpeg(scene().resize((320, 240))), "small.jpg")
        self.assertTrue(any("low resolution" in i for i in q.issues))

    def test_same_photo_resized_is_a_duplicate_but_different_photo_is_not(self):
        a = check_photo_quality(jpeg(scene(1)), "a.jpg")
        a_copy = check_photo_quality(_save(scene(1).resize((640, 480)), 60), "a_copy.jpg")
        other = check_photo_quality(jpeg(scene(2)), "other.jpg")
        self.assertEqual(find_duplicates([a, a_copy, other]), [("a.jpg", "a_copy.jpg")])


class SeverityTests(SimpleTestCase):
    def test_levels(self):
        self.assertEqual(estimate_severity("Small scratch on the mirror")[0], "minor")
        self.assertEqual(estimate_severity("Windshield shattered and door bent")[0], "moderate")
        self.assertEqual(estimate_severity("Car rolled over, airbags deployed")[0], "severe")

    def test_many_parts_raises_severity(self):
        self.assertEqual(estimate_severity("bumper, boot, headlight damaged")[0], "moderate")
        self.assertEqual(estimate_severity("bumper, boot, headlight, mirror, grille damaged")[0], "severe")

    def test_no_injuries_is_not_severe(self):
        self.assertEqual(estimate_severity("No one was injured. Scratch on bumper.")[0], "minor")

    def test_injury_is_severe(self):
        self.assertEqual(estimate_severity("Driver injured, taken to hospital")[0], "severe")

    def test_amount_can_raise_severity(self):
        self.assertEqual(estimate_severity("Scratch on door", 400000)[0], "severe")
        self.assertEqual(estimate_severity("Scratch", 60000)[0], "moderate")
        self.assertEqual(estimate_severity("Scratch", 5000)[0], "minor")

    def test_no_words_and_no_amount_gives_none(self):
        severity, reasons = estimate_severity("")
        self.assertIsNone(severity)
        self.assertEqual(reasons, ["no damage wording found"])

    def test_a_word_inside_another_word_does_not_count(self):
        self.assertIsNone(estimate_severity("The firewall of the office")[0])


class ConfidenceTests(SimpleTestCase):
    FULL = {
        "customer_name": "A", "policy_number": "P", "accident_description": "x",
        "damage_description": "y", "vehicle_details": {"make": "Tata"},
    }

    def test_everything_found(self):
        self.assertEqual(confidence_score(self.FULL, [0.9], [])[0], 0.9)

    def test_missing_fields_lower_it(self):
        score, parts = confidence_score({"customer_name": "A"}, [], [])
        self.assertEqual(parts["completeness"], 0.2)
        self.assertEqual(score, 0.2)

    def test_score_stays_between_zero_and_one(self):
        self.assertEqual(confidence_score({}, [], [])[0], 0.0)


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
@needs_tesseract
class DamagePipelineTests(TestCase):
    def setUp(self):
        self.claim = make_claim(
            make_user("devi"), description="Rear-ended at a signal. Bumper cracked.", amount_claimed=8000
        )

    def tearDown(self):
        from django.conf import settings

        shutil.rmtree(settings.MEDIA_ROOT, ignore_errors=True)

    def add(self, name, content):
        return ClaimDocument.objects.create(
            claim=self.claim, file=SimpleUploadedFile(name, content), original_name=name
        )

    def test_good_photo_and_form(self):
        self.add("form.png", image_bytes(form_image(FORM_LINES)))
        self.add("front.jpg", jpeg(scene(5)))
        ex = process_claim(self.claim)
        self.assertEqual([p["name"] for p in ex.analysis["photos"]], ["front.jpg"])  # the form is not a photo
        self.assertTrue(ex.analysis["photos"][0]["usable"])
        self.assertNotIn("No usable vehicle photos", " ".join(ex.review_reasons))
        self.assertEqual(ex.data["damage_severity"], "minor")
        self.assertGreater(ex.data["confidence"], 0)
        self.assertNotIn("fingerprint", ex.analysis["photos"][0])
        self.assertNotIn("no text found", " ".join(ex.review_reasons))  # normal for a photo

    def test_no_photos_is_warned(self):
        ex = process_claim(self.claim)
        self.assertTrue(any("No usable vehicle photos" in r for r in ex.review_reasons))

    def test_blurry_and_duplicate_photos_are_warned(self):
        self.add("blur.jpg", jpeg(scene(6).filter(ImageFilter.GaussianBlur(6))))
        self.add("a.jpg", jpeg(scene(7)))
        self.add("a_again.jpg", _save(scene(7).resize((800, 600)), 70))
        ex = process_claim(self.claim)
        text = " | ".join(ex.review_reasons)
        self.assertIn("blur.jpg: photo problem", text)
        self.assertIn("look like the same photo", text)
        self.assertLess(ex.analysis["confidence_parts"]["photos"], 1.0)

    def test_severe_claim_gets_a_reason(self):
        self.claim.amount_claimed = 500000
        self.claim.save()
        ex = process_claim(self.claim)
        self.assertEqual(ex.data["damage_severity"], "severe")
        self.assertTrue(any(r.startswith("Severe damage estimated") for r in ex.review_reasons))

    def test_corrupt_photo_does_not_crash(self):
        self.add("broken.jpg", b"not an image")
        ex = process_claim(self.claim)
        self.assertIn("not a readable image", ex.error_message)

    def test_a_textless_photo_does_not_zero_the_confidence(self):
        self.add("form.png", image_bytes(form_image(FORM_LINES)))
        self.add("front.jpg", jpeg(scene(9)))
        ex = process_claim(self.claim)
        self.assertGreater(ex.analysis["confidence_parts"]["ocr"], 0.6)  # the form's own OCR quality
