import shutil
import tempfile
from datetime import date, timedelta
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase, override_settings

from .ai.extractor import find_form_date
from .ai.pipeline import process_claim
from .ai.validation import parse_json_text, validate_extraction
from .models import ClaimDocument, ExtractedClaim
from .tests import make_claim, make_user

GOOD = {
    "customer_name": "Asha Rao",
    "policy_number": "POL-123456",
    "accident_description": "Hit from behind at a signal.",
    "vehicle_details": {"make": "Maruti", "model": "Swift", "year": 2019},
    "damage_description": "Rear bumper cracked",
    "images": [],
    "damage_severity": "minor",
    "confidence": 0.9,
}


def codes(report):
    return [i.code for i in report.issues]


class JsonSyntaxAndTypeTests(SimpleTestCase):
    def test_broken_json_gets_a_located_message(self):
        r = parse_json_text('{"customer_name": "Asha",\n "policy_number": }')
        self.assertEqual(codes(r), ["json_syntax"])
        self.assertIn("line 2", r.errors[0].message)
        self.assertTrue(r.has_structural_errors)

    def test_ai_style_fenced_json_is_accepted(self):
        r = parse_json_text('```json\n{"customer_name": "Asha Rao"}\n```')
        self.assertTrue(r.is_valid)
        self.assertEqual(r.data.customer_name, "Asha Rao")

    def test_empty_and_wrong_shape(self):
        self.assertEqual(codes(parse_json_text("")), ["json_syntax"])
        self.assertIn("got list", parse_json_text("[1, 2]").errors[0].message)

    def test_type_errors_name_the_field(self):
        r = parse_json_text('{"vehicle_details": {"year": "abcd"}, "confidence": 2, "damage_severity": "huge"}')
        fields = {i.field for i in r.errors}
        self.assertEqual(fields, {"vehicle_details.year", "confidence", "damage_severity"})
        self.assertTrue(r.has_structural_errors)

    def test_year_as_text_digits_is_converted_to_integer(self):
        r = parse_json_text('{"vehicle_details": {"year": "2019"}}')
        self.assertEqual(r.data.vehicle_details.year, 2019)


class FieldChecksTests(TestCase):
    def setUp(self):
        self.claim = make_claim(make_user("asha", first_name="Asha", last_name="Rao"))

    def run_validation(self, data=None, typed="", **kw):
        return validate_extraction(self.claim, data if data is not None else GOOD, typed, **kw)

    def test_clean_claim_has_no_errors_or_warnings(self):
        r = self.run_validation(typed="Maruti Swift 2019 was hit from behind. Bumper cracked.")
        self.assertTrue(r.is_valid)
        self.assertEqual(r.warnings, [])

    def test_missing_required_fields_are_errors(self):
        r = self.run_validation({**GOOD, "customer_name": None, "policy_number": None})
        self.assertEqual(r.missing[:2], ["customer_name", "policy_number"])
        self.assertFalse(r.is_valid)
        self.assertFalse(r.has_structural_errors)  # a person can fix this; the JSON is fine
        self.assertIn("Customer name was not found in the documents or description.", r.reasons())

    def test_missing_optional_fields_are_info_only(self):
        r = self.run_validation({**GOOD, "damage_description": None, "vehicle_details": {}})
        self.assertTrue(r.is_valid)
        self.assertIn("vehicle_details.year", r.missing)
        self.assertEqual(r.reasons(), [])

    def test_bad_policy_format(self):
        r = self.run_validation({**GOOD, "policy_number": "AB1"})
        self.assertIn("policy_format", codes(r))

    def test_unserialisable_data_is_reported_not_raised(self):
        r = self.run_validation({"customer_name": {1, 2}})
        self.assertEqual(codes(r), ["json_syntax"])


class ConsistencyTests(TestCase):
    def setUp(self):
        self.claim = make_claim(make_user("asha", first_name="Asha", last_name="Rao"))

    def check(self, data=None, typed="", **kw):
        return validate_extraction(self.claim, data or GOOD, typed, **kw)

    def test_policy_number_mismatch_with_claim_form(self):
        self.claim.policy_number = "POL-999999"
        self.assertIn("policy_mismatch", codes(self.check()))

    def test_name_mismatch_with_account(self):
        r = self.check({**GOOD, "customer_name": "Ravi Kumar"})
        self.assertIn("name_mismatch", codes(r))

    def test_partial_name_overlap_is_fine_and_unnamed_account_is_skipped(self):
        self.assertNotIn("name_mismatch", codes(self.check({**GOOD, "customer_name": "ASHA RAO K"})))
        self.claim.claimant.first_name = self.claim.claimant.last_name = ""
        self.assertNotIn("name_mismatch", codes(self.check({**GOOD, "customer_name": "Ravi Kumar"})))

    def test_date_mismatch_invalid_and_future(self):
        self.assertIn("date_mismatch", codes(self.check(form_date=date(2020, 1, 1), form_date_raw="01/01/2020")))
        self.assertIn("invalid_date", codes(self.check(form_date=None, form_date_raw="31/02/2024")))
        tomorrow = date.today() + timedelta(days=1)
        self.assertIn("future_date", codes(self.check(form_date=tomorrow, form_date_raw="x")))

    def test_matching_date_is_fine(self):
        r = self.check(form_date=self.claim.incident_date, form_date_raw="x")
        self.assertEqual(r.warnings, [])

    def test_vehicle_make_mismatch_with_typed_description(self):
        r = self.check(typed="My Honda City was hit from behind.")
        self.assertIn("vehicle_mismatch", codes(r))
        self.assertIn("Honda", r.warnings[0].message)

    def test_maruti_and_maruti_suzuki_are_the_same_make(self):
        r = self.check({**GOOD, "vehicle_details": {"make": "Maruti Suzuki", "model": "Swift", "year": 2019}},
                       "Maruti Swift 2019 hit.")
        self.assertEqual([c for c in codes(r) if c == "vehicle_mismatch"], [])

    def test_vehicle_model_and_year_mismatch(self):
        self.assertIn("vehicle_details.model", [i.field for i in self.check(typed="Maruti Baleno was hit").issues])
        self.assertIn("vehicle_details.year", [i.field for i in self.check(typed="Maruti Swift 2015 was hit").issues])

    def test_damaged_parts_that_do_not_overlap(self):
        r = self.check(typed="Windshield shattered and left mirror broken.")  # form says bumper
        self.assertIn("damage_mismatch", codes(r))

    def test_overlapping_parts_are_fine(self):
        self.assertNotIn("damage_mismatch", codes(self.check(typed="Bumper and headlight damaged.")))


class FormDateTests(SimpleTestCase):
    def test_formats(self):
        for text in ("12/03/2024", "12-03-2024", "12.03.2024", "2024-03-12", "12 March 2024", "12th Mar 2024", "March 12, 2024"):
            self.assertEqual(find_form_date(f"Date of Accident: {text}")[0], date(2024, 3, 12), text)

    def test_day_first_order(self):
        self.assertEqual(find_form_date("Date of Incident: 05/04/2024")[0], date(2024, 4, 5))

    def test_unparseable_date_returns_the_raw_text(self):
        self.assertEqual(find_form_date("Date of Accident: 31/02/2024"), (None, "31/02/2024"))

    def test_absent(self):
        self.assertEqual(find_form_date("Policy Number: POL-1"), (None, None))

    def test_side_by_side_on_one_line(self):
        self.assertEqual(find_form_date("Customer Name: Asha Rao    Date of Accident: 12/03/2024")[0], date(2024, 3, 12))


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class PipelineValidationTests(TestCase):
    def setUp(self):
        self.claim = make_claim(make_user("devi", first_name="Devi", last_name="Nair"),
                                description="Rear-ended at a signal. Bumper cracked.")

    def tearDown(self):
        from django.conf import settings

        shutil.rmtree(settings.MEDIA_ROOT, ignore_errors=True)

    def add_text(self, text, name="form.txt"):
        return ClaimDocument.objects.create(
            claim=self.claim, file=SimpleUploadedFile(name, text.encode()), original_name=name)

    def test_mismatches_reach_the_review_list(self):
        self.add_text("Customer Name: Devi Nair\nPolicy Number: POL-777777\nDate of Accident: 01/01/2020\n"
                      "Vehicle: Maruti Swift 2019\nDamage: Rear bumper cracked")
        ex = process_claim(self.claim)
        text = " | ".join(ex.review_reasons)
        self.assertIn("Policy number mismatch", text)
        self.assertIn("Date mismatch", text)
        self.assertEqual(ex.status, ExtractedClaim.Status.NEEDS_REVIEW)  # mismatches are flagged for a person
        self.assertEqual(ex.priority, "high")  # policy and date mismatches are urgent
        self.assertTrue(ex.analysis["validation"]["valid"])

    def test_missing_name_listed_once_and_sent_to_review(self):
        self.add_text("Policy Number: POL-123456")
        ex = process_claim(self.claim)
        self.assertEqual(sum("Customer name was not found" in r for r in ex.review_reasons), 1)
        self.assertEqual(ex.status, ExtractedClaim.Status.NEEDS_REVIEW)

    def test_broken_json_marks_the_extraction_failed(self):
        self.add_text("Customer Name: Devi Nair\nPolicy Number: POL-123456")
        with mock.patch("claims.ai.pipeline.confidence_score", return_value=(5, {})):  # 5 is outside 0-1
            ex = process_claim(self.claim)
        self.assertEqual(ex.status, ExtractedClaim.Status.FAILED)
        self.assertIn("Field 'confidence' is invalid", ex.error_message)

    def test_saved_json_validates_when_reloaded(self):
        self.add_text("Customer Name: Devi Nair\nPolicy Number: POL-123456")
        ex = process_claim(self.claim)
        ex.refresh_from_db()
        self.assertTrue(parse_json_text(__import__("json").dumps(ex.data)).is_valid)
