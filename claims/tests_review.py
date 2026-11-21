import shutil
import tempfile
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from accounts.models import User

from .ai.pipeline import maybe_auto_process, process_claim
from .ai.review import Flag, ReviewError, apply_review, changed_fields, decide
from .models import Claim, ClaimDocument, ExtractedClaim
from .tests import VALID_FORM, make_claim, make_user
from .tests_ingestion import FORM_LINES, form_image, image_bytes, needs_tesseract
from .tests_damage import jpeg, scene

Status = ExtractedClaim.Status
CLEAN_FORM = "Customer Name: Devi Nair\nPolicy Number: POL-123456\nVehicle: Maruti Swift 2019\nDamage: Rear bumper cracked"


class DecisionRuleTests(SimpleTestCase):
    def test_no_flags_passes_automatic_checks(self):
        d = decide([])
        self.assertFalse(d.flagged)
        self.assertEqual(d.status, Status.AUTO_VERIFIED)
        self.assertEqual(d.priority, "normal")

    def test_any_flag_needs_review(self):
        d = decide([Flag("photo_problem", "x.jpg: blurry")])
        self.assertEqual((d.status, d.priority), (Status.NEEDS_REVIEW, "normal"))

    def test_fraud_signals_are_high_priority(self):
        for code in ("severe_damage", "policy_mismatch", "name_mismatch", "date_mismatch", "future_date", "duplicate_photo"):
            self.assertEqual(decide([Flag("photo_problem", "a"), Flag(code, "b")]).priority, "high", code)

    def test_repeated_messages_are_shown_once(self):
        d = decide([Flag("missing_required", "Same text."), Flag("extraction_uncertain", "Same text.")])
        self.assertEqual(len(d.flags), 1)
        self.assertEqual(d.flags[0].code, "missing_required")  # the first (more specific) flag wins


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class PipelineDecisionTests(TestCase):
    def setUp(self):
        # a health claim needs no vehicle photos, so a clean text form can pass every check
        self.claim = make_claim(make_user("devi", first_name="Devi", last_name="Nair"),
                                claim_type=Claim.ClaimType.HEALTH, description="Rear-ended at a signal. Bumper cracked.")

    def tearDown(self):
        from django.conf import settings

        shutil.rmtree(settings.MEDIA_ROOT, ignore_errors=True)

    def add_text(self, text, name="form.txt"):
        return ClaimDocument.objects.create(claim=self.claim, file=SimpleUploadedFile(name, text.encode()), original_name=name)

    def test_clean_claim_passes_automatically(self):
        self.add_text(CLEAN_FORM)
        ex = process_claim(self.claim)
        self.assertEqual(ex.status, Status.AUTO_VERIFIED, ex.review_reasons)
        self.assertEqual(ex.review_reasons, [])
        self.assertFalse(ex.analysis["review"]["flagged"])

    def test_severe_damage_is_flagged_high(self):
        self.add_text(CLEAN_FORM + "\nAccident: Car rolled over, airbags deployed")
        ex = process_claim(self.claim)
        self.assertEqual((ex.status, ex.priority), (Status.NEEDS_REVIEW, "high"))
        self.assertTrue(any(r.startswith("Severe damage estimated") for r in ex.review_reasons))

    def test_low_confidence_is_flagged(self):
        self.add_text(CLEAN_FORM.replace("Vehicle: Maruti Swift 2019\n", ""))  # no vehicle: confidence 0.8
        with override_settings(REVIEW_MIN_CONFIDENCE=0.9):
            ex = process_claim(self.claim)
        self.assertEqual(ex.status, Status.NEEDS_REVIEW)
        self.assertTrue(any(r.startswith("Low confidence") for r in ex.review_reasons))

    def test_policy_mismatch_is_flagged_high(self):
        self.add_text(CLEAN_FORM.replace("POL-123456", "POL-999999"))
        ex = process_claim(self.claim)
        self.assertEqual((ex.status, ex.priority), (Status.NEEDS_REVIEW, "high"))

    def test_car_claim_without_photos_is_flagged(self):
        self.claim.claim_type = Claim.ClaimType.AUTO
        self.claim.save()
        self.add_text(CLEAN_FORM)
        ex = process_claim(self.claim)
        self.assertEqual(ex.status, Status.NEEDS_REVIEW)
        self.assertEqual(ex.priority, "normal")

    @needs_tesseract
    def test_duplicate_photos_are_high_priority(self):
        self.claim.claim_type = Claim.ClaimType.AUTO
        self.claim.save()
        self.add_text(CLEAN_FORM)
        for name in ("a.jpg", "b.jpg"):
            ClaimDocument.objects.create(claim=self.claim, file=SimpleUploadedFile(name, jpeg(scene(3))), original_name=name)
        ex = process_claim(self.claim)
        self.assertEqual((ex.status, ex.priority), (Status.NEEDS_REVIEW, "high"))
        self.assertTrue(any("look like the same photo" in r for r in ex.review_reasons))

    def test_human_decision_is_not_overwritten_but_system_pass_is(self):
        self.add_text(CLEAN_FORM)
        ex = process_claim(self.claim)
        reviewer = make_user("rev", role=User.Role.AGENT)
        apply_review(ex, reviewer, "approve")
        self.add_text("Policy Number: POL-555555", "extra.txt")
        self.assertEqual(process_claim(self.claim).status, Status.VERIFIED)  # untouched
        self.assertEqual(process_claim(self.claim, reprocess=True).reviewed_by, None)  # explicit re-run resets it

    def test_auto_process_setting(self):
        self.add_text(CLEAN_FORM)
        self.assertIsNone(maybe_auto_process(self.claim))  # off by default
        self.assertFalse(ExtractedClaim.objects.exists())
        with override_settings(AI_AUTO_PROCESS=True):
            self.assertIsNotNone(maybe_auto_process(self.claim))
        self.assertTrue(ExtractedClaim.objects.exists())

    def test_auto_process_never_breaks_a_submission(self):
        with override_settings(AI_AUTO_PROCESS=True), mock.patch("claims.ai.pipeline.process_claim", side_effect=RuntimeError("boom")):
            with self.assertLogs("claims.ai.pipeline", level="ERROR"):
                self.assertIsNone(maybe_auto_process(self.claim))


class ApplyReviewTests(TestCase):
    def setUp(self):
        self.claim = make_claim(make_user("owner"), description="Rear-ended at a signal. Bumper cracked.")
        self.reviewer = make_user("agent1", role=User.Role.AGENT)
        self.ex = ExtractedClaim.objects.create(
            claim=self.claim, status=Status.NEEDS_REVIEW,
            data={"customer_name": None, "policy_number": "POL-123456", "vehicle_details": {"make": "Tata", "model": None, "year": None},
                  "accident_description": "x", "damage_description": "y", "images": []},
        )

    def test_cannot_approve_while_an_essential_field_is_missing(self):
        with self.assertRaisesMessage(ReviewError, "Customer name was not found"):
            apply_review(self.ex, self.reviewer, "approve")
        self.ex.refresh_from_db()
        self.assertEqual(self.ex.status, Status.NEEDS_REVIEW)

    def test_correcting_and_approving_keeps_the_ai_version(self):
        apply_review(self.ex, self.reviewer, "approve",
                     edits={"customer_name": "Asha Rao", "vehicle_year": "2019"}, note="Name was on page 2")
        self.ex.refresh_from_db()
        self.assertEqual(self.ex.status, Status.VERIFIED)
        self.assertEqual(self.ex.data["customer_name"], "Asha Rao")
        self.assertEqual(self.ex.data["vehicle_details"]["year"], 2019)  # converted to an integer
        self.assertIsNone(self.ex.original_data["customer_name"])  # what the AI said is kept
        self.assertEqual((self.ex.reviewed_by, self.ex.review_note), (self.reviewer, "Name was on page 2"))
        self.assertIsNotNone(self.ex.reviewed_at)
        self.assertEqual(changed_fields(self.ex.original_data, self.ex.data)[0][0], "Customer name")

    def test_approving_without_changes_leaves_no_edit_history(self):
        self.ex.data["customer_name"] = "Asha Rao"
        self.ex.save()
        apply_review(self.ex, self.reviewer, "approve", edits={"customer_name": "Asha Rao"})
        self.ex.refresh_from_db()
        self.assertEqual(self.ex.original_data, {})

    def test_bad_edit_is_refused_with_the_field_named(self):
        with self.assertRaisesMessage(ReviewError, "vehicle_details.year"):
            apply_review(self.ex, self.reviewer, "approve", edits={"customer_name": "A", "vehicle_year": "abcd"})

    def test_reject_needs_a_reason(self):
        with self.assertRaisesMessage(ReviewError, "reason for rejecting"):
            apply_review(self.ex, self.reviewer, "reject", note="  ")
        apply_review(self.ex, self.reviewer, "reject", note="Documents belong to another policy")
        self.ex.refresh_from_db()
        self.assertEqual(self.ex.status, Status.REJECTED)

    def test_reopen_clears_the_decision(self):
        apply_review(self.ex, self.reviewer, "reject", note="no")
        apply_review(self.ex, self.reviewer, "reopen")
        self.ex.refresh_from_db()
        self.assertEqual((self.ex.status, self.ex.reviewed_by), (Status.NEEDS_REVIEW, None))

    def test_unknown_action(self):
        with self.assertRaises(ReviewError):
            apply_review(self.ex, self.reviewer, "explode")


class ReviewScreenTests(TestCase):
    def setUp(self):
        self.owner = make_user("owner")
        self.agent = make_user("agent1", role=User.Role.AGENT)
        self.other_agent = make_user("agent2", role=User.Role.AGENT)
        self.admin = make_user("boss", role=User.Role.ADMIN)
        self.mine = make_claim(self.owner)
        self.hidden = make_claim(self.owner, assigned_agent=self.other_agent)
        base = {"customer_name": "Asha Rao", "policy_number": "POL-123456", "accident_description": "a", "damage_description": "d",
                "vehicle_details": {"make": "Tata", "model": "Nexon", "year": 2020}, "images": [], "damage_severity": "minor", "confidence": 0.8}
        self.ex = ExtractedClaim.objects.create(
            claim=self.mine, status=Status.NEEDS_REVIEW, priority="normal", data=base, review_reasons=["Policy number mismatch"],
            analysis={"review": {"flagged": True, "priority": "normal", "flags": [{"code": "policy_mismatch", "message": "Policy number mismatch", "priority": "high"}]}})
        ExtractedClaim.objects.create(claim=self.hidden, status=Status.NEEDS_REVIEW, priority="high", data=base)

    def test_claimants_cannot_open_the_review_pages(self):
        self.client.force_login(self.owner)
        for name, args in (("claims:review_queue", []), ("claims:review_detail", [self.mine.pk])):
            self.assertEqual(self.client.get(reverse(name, args=args)).status_code, 403)
        self.assertEqual(self.client.post(reverse("claims:review_run", args=[self.mine.pk])).status_code, 403)

    def test_agent_sees_only_visible_claims_and_urgent_first(self):
        self.client.force_login(self.agent)
        r = self.client.get(reverse("claims:review_queue"))
        self.assertContains(r, self.mine.claim_number)
        self.assertNotContains(r, self.hidden.claim_number)
        self.client.force_login(self.admin)
        html = self.client.get(reverse("claims:review_queue")).content.decode()
        self.assertLess(html.index(self.hidden.claim_number), html.index(self.mine.claim_number))  # high priority first

    def test_status_tabs_filter_the_queue(self):
        self.client.force_login(self.admin)
        self.assertNotContains(self.client.get(reverse("claims:review_queue") + "?status=verified"), self.mine.claim_number)
        self.assertContains(self.client.get(reverse("claims:review_queue") + "?status=garbage"), self.mine.claim_number)

    def test_detail_hidden_claim_is_404(self):
        self.client.force_login(self.agent)
        self.assertEqual(self.client.get(reverse("claims:review_detail", args=[self.hidden.pk])).status_code, 404)

    def test_detail_page_shows_flags_and_form(self):
        self.client.force_login(self.agent)
        r = self.client.get(reverse("claims:review_detail", args=[self.mine.pk]))
        self.assertContains(r, "Policy number mismatch")
        self.assertContains(r, 'value="Asha Rao"')
        self.assertContains(r, "Approve (with any corrections)")

    def test_approve_with_a_correction_through_the_page(self):
        self.client.force_login(self.agent)
        r = self.client.post(reverse("claims:review_detail", args=[self.mine.pk]),
                             {"action": "approve", "customer_name": "Asha K Rao", "policy_number": "POL-123456", "vehicle_year": "2020", "note": "ok"})
        self.assertRedirects(r, reverse("claims:review_queue"))
        self.ex.refresh_from_db()
        self.assertEqual((self.ex.status, self.ex.reviewed_by, self.ex.data["customer_name"]), (Status.VERIFIED, self.agent, "Asha K Rao"))

    def test_reject_without_reason_shows_an_error_and_changes_nothing(self):
        self.client.force_login(self.agent)
        r = self.client.post(reverse("claims:review_detail", args=[self.mine.pk]), {"action": "reject", "customer_name": "x"}, follow=False)
        self.assertContains(r, "reason for rejecting")
        self.ex.refresh_from_db()
        self.assertEqual(self.ex.status, Status.NEEDS_REVIEW)

    def test_decided_item_shows_the_decision_and_can_be_reopened(self):
        apply_review(self.ex, self.agent, "reject", note="wrong policy")
        self.client.force_login(self.agent)
        r = self.client.get(reverse("claims:review_detail", args=[self.mine.pk]))
        self.assertContains(r, "wrong policy")
        self.assertNotContains(r, "Approve (with any corrections)")
        self.client.post(reverse("claims:review_detail", args=[self.mine.pk]), {"action": "reopen"})
        self.ex.refresh_from_db()
        self.assertEqual(self.ex.status, Status.NEEDS_REVIEW)

    def test_run_button_creates_an_extraction_and_pending_runs_the_rest(self):
        fresh = make_claim(self.owner, description="Rear-ended at a signal. Bumper cracked.")
        self.client.force_login(self.admin)
        self.client.post(reverse("claims:review_run", args=[fresh.pk]))
        self.assertTrue(ExtractedClaim.objects.filter(claim=fresh).exists())
        fresh2 = make_claim(self.owner, description="Hit by a truck at the junction. Door dented.")
        self.client.post(reverse("claims:review_run_pending"))
        self.assertTrue(ExtractedClaim.objects.filter(claim=fresh2).exists())

    def test_run_button_refuses_to_overwrite_a_human_decision(self):
        apply_review(self.ex, self.agent, "reject", note="no")
        self.client.force_login(self.agent)
        r = self.client.post(reverse("claims:review_run", args=[self.mine.pk]), follow=True)
        self.assertContains(r, "Re-open it first")
        self.ex.refresh_from_db()
        self.assertEqual(self.ex.status, Status.REJECTED)

    def test_staff_nav_link_and_claim_page_link(self):
        self.client.force_login(self.agent)
        self.assertContains(self.client.get(reverse("claims:dashboard")), "AI review")
        self.assertContains(self.client.get(reverse("claims:detail", args=[self.mine.pk])), "AI extraction review")
        self.client.force_login(self.owner)
        self.assertNotContains(self.client.get(reverse("claims:dashboard")), "AI review")


@override_settings(MEDIA_ROOT=tempfile.mkdtemp(), AI_AUTO_PROCESS=True)
class AutoProcessOnSubmitTests(TestCase):
    def test_submitting_a_claim_runs_the_check_when_enabled(self):
        user = make_user("auto")
        self.client.force_login(user)
        r = self.client.post(reverse("claims:create"), VALID_FORM)
        self.assertEqual(r.status_code, 302)
        self.assertTrue(ExtractedClaim.objects.filter(claim__claimant=user).exists())

    def test_a_crash_in_the_check_does_not_lose_the_claim(self):
        user = make_user("auto2")
        self.client.force_login(user)
        with mock.patch("claims.ai.pipeline.process_claim", side_effect=RuntimeError("boom")), \
                self.assertLogs("claims.ai.pipeline", level="ERROR"):
            r = self.client.post(reverse("claims:create"), VALID_FORM)
        self.assertEqual(r.status_code, 302)
        self.assertTrue(Claim.objects.filter(claimant=user).exists())
