from datetime import date

from django.test import TestCase
from pydantic import ValidationError

from .ai.schema import ExtractedClaimData
from .models import ExtractedClaim
from .tests import make_claim, make_user


class SchemaTests(TestCase):
    def test_matches_brief_format(self):
        data = ExtractedClaimData(
            customer_name="Asha Rao",
            policy_number="pol-123456",
            vehicle_details={"make": "Maruti", "model": "Swift", "year": "2019"},
        ).to_json_dict()
        self.assertEqual(data["policy_number"], "POL-123456")
        self.assertEqual(data["vehicle_details"]["year"], 2019)  # string -> int
        for key in ("customer_name", "policy_number", "accident_description",
                    "vehicle_details", "damage_description", "images"):
            self.assertIn(key, data)

    def test_missing_values_become_null(self):
        d = ExtractedClaimData(customer_name="  ", policy_number="N/A")
        self.assertIsNone(d.customer_name)
        self.assertIsNone(d.policy_number)
        self.assertEqual(d.missing_required(), ["customer_name", "policy_number"])
        self.assertIsNone(d.to_json_dict()["vehicle_details"]["year"])

    def test_bad_year_rejected(self):
        with self.assertRaises(ValidationError):
            ExtractedClaimData(vehicle_details={"year": "abcd"})
        with self.assertRaises(ValidationError):
            ExtractedClaimData(vehicle_details={"year": date.today().year + 5})

    def test_bad_json_rejected(self):
        with self.assertRaises(ValidationError):
            ExtractedClaimData.model_validate_json("{not valid json")

    def test_unknown_fields_ignored_and_confidence_bounded(self):
        d = ExtractedClaimData.model_validate({"surprise": 1, "confidence": 0.8})
        self.assertEqual(d.confidence, 0.8)
        with self.assertRaises(ValidationError):
            ExtractedClaimData(confidence=1.5)


class ExtractedClaimModelTests(TestCase):
    def test_store_json_on_claim(self):
        claim = make_claim(make_user("sam"))
        payload = ExtractedClaimData(customer_name="Sam", policy_number="POL-1").to_json_dict()
        ex = ExtractedClaim.objects.create(claim=claim, data=payload)
        ex.refresh_from_db()
        self.assertEqual(ex.status, ExtractedClaim.Status.PROCESSING)
        self.assertEqual(ex.data["customer_name"], "Sam")
        self.assertEqual(claim.extraction, ex)
