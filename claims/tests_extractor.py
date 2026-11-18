import json
import shutil
import tempfile

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from .ai.extractor import extract_fields
from .ai.pipeline import process_claim
from .ai.schema import ExtractedClaimData
from .models import ClaimDocument, ExtractedClaim
from .tests import make_claim, make_user
from .tests_ingestion import FORM_LINES, form_image, image_bytes, needs_tesseract

CLEAN_FORM = """Customer Name: Asha Rao
Policy Number: POL-123456
Vehicle: Maruti Swift, Year 2019
Accident: Hit from behind at signal, Silk Board, Bengaluru
Damage: Rear bumper cracked, boot lid dented"""


class ExtractorTests(SimpleTestCase):
    def fields(self, form="", typed=""):
        outcome = extract_fields(form, typed)
        return outcome.data.to_json_dict(), outcome

    def test_clean_form(self):
        d, o = self.fields(CLEAN_FORM)
        self.assertEqual(d["customer_name"], "Asha Rao")
        self.assertEqual(d["policy_number"], "POL-123456")
        self.assertEqual(d["vehicle_details"], {"make": "Maruti", "model": "Swift", "year": 2019})
        self.assertEqual(d["damage_description"], "Rear bumper cracked, boot lid dented")
        self.assertEqual(o.warnings, [])

    def test_output_is_valid_json_in_brief_format(self):
        d, _ = self.fields(CLEAN_FORM)
        reloaded = ExtractedClaimData.model_validate_json(json.dumps(d))
        self.assertEqual(reloaded.policy_number, "POL-123456")

    def test_uppercase_columns_on_one_line(self):
        d, _ = self.fields(
            "NAME OF INSURED : RAVI KUMAR    POLICY NO. : MOT/2024/778899\n"
            "MAKE : HYUNDAI   MODEL : i20   YEAR OF MANUFACTURE : 2021\n"
            "DESCRIPTION OF ACCIDENT : Car skidded on wet road\nand hit a divider.\n\n"
            "DAMAGE DETAILS : Front bumper broken"
        )
        self.assertEqual(d["customer_name"], "Ravi Kumar")
        self.assertEqual(d["policy_number"], "MOT-2024-778899")
        self.assertEqual(d["vehicle_details"], {"make": "Hyundai", "model": "i20", "year": 2021})
        self.assertEqual(d["accident_description"], "Car skidded on wet road and hit a divider.")

    def test_single_space_between_fields(self):
        d, o = self.fields("Customer Name: Asha Rao Policy Number: POL-123456")
        self.assertEqual((d["customer_name"], d["policy_number"]), ("Asha Rao", "POL-123456"))
        self.assertEqual(o.sources["policy_number"], "form")

    def test_separate_make_model_year_labels(self):
        d, _ = self.fields("Make: Tata\nModel: Nexon EV\nYear: 2022")
        self.assertEqual(d["vehicle_details"], {"make": "Tata", "model": "Nexon EV", "year": 2022})

    def test_multi_word_make_and_model(self):
        d, _ = self.fields("Vehicle : Maruti Suzuki Swift Dzire 2019")
        self.assertEqual(d["vehicle_details"], {"make": "Maruti Suzuki", "model": "Swift Dzire", "year": 2019})

    def test_insurance_company_line_is_not_the_vehicle_make(self):
        d, _ = self.fields("Insurance Company: ABC General\nCustomer Name: Meena")
        self.assertIsNone(d["vehicle_details"]["make"])

    def test_typed_description_is_the_fallback(self):
        d, o = self.fields("", "Honda City hit by a truck. Policy POL-445566. Rear bumper dented.")
        self.assertEqual(d["policy_number"], "POL-445566")
        self.assertEqual(d["vehicle_details"]["make"], "Honda")
        self.assertEqual(d["damage_description"], "Rear bumper dented.")
        self.assertEqual(o.sources["accident_description"], "typed")
        self.assertTrue(any("found by pattern" in w for w in o.warnings))

    def test_missing_values_are_null_and_warned(self):
        d, o = self.fields("Customer Name: Meena Iyer", "Rear ended.")
        self.assertIsNone(d["policy_number"])
        self.assertEqual(o.data.missing_required(), ["policy_number"])
        self.assertIn("Policy number was not found in the documents or description.", o.warnings)

    def test_empty_input(self):
        d, o = self.fields("", "")
        self.assertEqual(o.data.missing_required(), ["customer_name", "policy_number"])
        self.assertIsNone(d["vehicle_details"]["year"])

    def test_name_with_digits_rejected_once(self):
        _, o = self.fields("Customer Name: R4vi Kum4r\nPolicy Number: AB1234")
        self.assertIsNone(o.data.customer_name)
        self.assertEqual(sum("Customer name" in w for w in o.warnings), 1)

    def test_impossible_year_warned_and_kept_out_of_model(self):
        d, o = self.fields("Vehicle: Maruti Swift 1899\nYear: 1899")
        self.assertIsNone(d["vehicle_details"]["year"])
        self.assertEqual(d["vehicle_details"]["model"], "Swift")
        self.assertTrue(any("not a realistic year" in w for w in o.warnings))

    def test_policy_cleaned_up_gets_warning(self):
        d, o = self.fields("Policy Number : POL 123456")
        self.assertEqual(d["policy_number"], "POL123456")
        self.assertTrue(any("cleaned up" in w for w in o.warnings))

    def test_too_short_policy_is_not_accepted(self):
        d, _ = self.fields("Policy Number: AB12")
        self.assertIsNone(d["policy_number"])


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class PipelineTests(TestCase):
    def setUp(self):
        self.claim = make_claim(make_user("kiran"), description="Hit from behind at a signal. Bumper cracked.")

    def tearDown(self):
        shutil.rmtree(self.settings_root(), ignore_errors=True)

    def settings_root(self):
        from django.conf import settings

        return settings.MEDIA_ROOT

    @needs_tesseract
    def test_image_to_saved_json(self):
        doc = ClaimDocument.objects.create(
            claim=self.claim,
            file=SimpleUploadedFile("form.png", image_bytes(form_image(FORM_LINES))),
            original_name="form.png",
        )
        extraction = process_claim(self.claim)
        extraction.refresh_from_db()
        self.assertEqual(extraction.data["customer_name"], "Asha Rao")
        self.assertEqual(extraction.data["policy_number"], "POL-123456")
        self.assertEqual(extraction.data["vehicle_details"]["year"], 2019)
        self.assertEqual(extraction.data["images"], [reverse("claims:document", args=[doc.pk])])
        self.assertEqual(extraction.status, ExtractedClaim.Status.PROCESSING)

    def test_missing_essentials_go_to_review(self):
        extraction = process_claim(self.claim)  # no documents, no names in the description
        self.assertEqual(extraction.status, ExtractedClaim.Status.NEEDS_REVIEW)
        self.assertTrue(any("Customer name" in r for r in extraction.review_reasons))

    def test_running_twice_updates_the_same_row(self):
        process_claim(self.claim)
        process_claim(self.claim)
        self.assertEqual(ExtractedClaim.objects.filter(claim=self.claim).count(), 1)

    def test_bad_file_is_reported_not_fatal(self):
        ClaimDocument.objects.create(
            claim=self.claim, file=SimpleUploadedFile("broken.png", b"garbage"), original_name="broken.png"
        )
        extraction = process_claim(self.claim)
        self.assertIn("not a readable image", extraction.error_message)
        self.assertEqual(extraction.status, ExtractedClaim.Status.NEEDS_REVIEW)
