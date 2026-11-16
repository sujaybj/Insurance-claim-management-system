import shutil
import tempfile
from datetime import date, timedelta

from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from accounts.models import User

from .models import Claim

TMP_MEDIA = tempfile.mkdtemp()


def make_user(username, role=User.Role.CLAIMANT, **kw):
    return User.objects.create_user(
        username=username, email=f"{username}@example.com", password="Passw0rd!x", role=role, **kw
    )


def make_claim(claimant, **kw):
    data = dict(
        claimant=claimant,
        policy_number="POL-123456",
        claim_type=Claim.ClaimType.AUTO,
        incident_date=date.today() - timedelta(days=3),
        amount_claimed=5000,
        description="Rear-ended at a traffic signal, bumper damaged.",
    )
    data.update(kw)
    return Claim.objects.create(**data)


VALID_FORM = {
    "policy_number": "pol-987654",
    "claim_type": "auto",
    "incident_date": (date.today() - timedelta(days=2)).isoformat(),
    "amount_claimed": "2500.00",
    "description": "Windshield cracked by a stone on the highway.",
}


@override_settings(MEDIA_ROOT=TMP_MEDIA)
class ClaimTestBase(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(TMP_MEDIA, ignore_errors=True)

    def setUp(self):
        self.alice = make_user("alice", phone="+919800000000")
        self.bob = make_user("bob")
        self.agent = make_user("agent1", User.Role.AGENT)
        self.admin = make_user("boss", User.Role.ADMIN)


class AuthTests(ClaimTestBase):
    def test_registration_creates_claimant_even_if_role_posted(self):
        r = self.client.post(
            reverse("accounts:register"),
            {
                "username": "newbie", "email": "new@example.com",
                "password1": "Str0ng-pass!9", "password2": "Str0ng-pass!9",
                "role": "admin",  # must be ignored
            },
        )
        self.assertEqual(r.status_code, 302)
        self.assertEqual(User.objects.get(username="newbie").role, User.Role.CLAIMANT)

    def test_duplicate_email_rejected(self):
        r = self.client.post(
            reverse("accounts:register"),
            {"username": "x", "email": "ALICE@example.com",
             "password1": "Str0ng-pass!9", "password2": "Str0ng-pass!9"},
        )
        self.assertEqual(r.status_code, 200)
        self.assertFalse(User.objects.filter(username="x").exists())

    def test_login_required_for_dashboard(self):
        r = self.client.get(reverse("claims:dashboard"))
        self.assertEqual(r.status_code, 302)
        self.assertIn("/accounts/login/", r["Location"])


class ClaimSubmissionTests(ClaimTestBase):
    def test_submit_with_documents(self):
        self.client.force_login(self.alice)
        f = SimpleUploadedFile("receipt.pdf", b"%PDF-1.4 test", content_type="application/pdf")
        r = self.client.post(reverse("claims:create"), {**VALID_FORM, "documents": [f]})
        self.assertEqual(r.status_code, 302)
        claim = Claim.objects.get()
        self.assertEqual(claim.claimant, self.alice)
        self.assertEqual(claim.policy_number, "POL-987654")
        self.assertEqual(claim.status, Claim.Status.RECEIVED)
        self.assertEqual(claim.documents.count(), 1)
        self.assertEqual(len(mail.outbox), 1)  # confirmation email

    def test_future_date_rejected(self):
        self.client.force_login(self.alice)
        bad = {**VALID_FORM, "incident_date": (date.today() + timedelta(days=1)).isoformat()}
        r = self.client.post(reverse("claims:create"), bad)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(Claim.objects.count(), 0)

    def test_short_description_and_bad_policy_rejected(self):
        self.client.force_login(self.alice)
        bad = {**VALID_FORM, "description": "too short", "policy_number": "!!"}
        r = self.client.post(reverse("claims:create"), bad)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(Claim.objects.count(), 0)

    def test_disallowed_file_type_rejected(self):
        self.client.force_login(self.alice)
        f = SimpleUploadedFile("evil.exe", b"MZ", content_type="application/octet-stream")
        r = self.client.post(reverse("claims:create"), {**VALID_FORM, "documents": [f]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(Claim.objects.count(), 0)

    def test_oversize_file_rejected(self):
        self.client.force_login(self.alice)
        f = SimpleUploadedFile("big.jpg", b"0" * (5 * 1024 * 1024 + 1), content_type="image/jpeg")
        r = self.client.post(reverse("claims:create"), {**VALID_FORM, "documents": [f]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(Claim.objects.count(), 0)

    def test_agent_cannot_submit_claims(self):
        self.client.force_login(self.agent)
        r = self.client.post(reverse("claims:create"), VALID_FORM)
        self.assertEqual(r.status_code, 302)
        self.assertEqual(Claim.objects.count(), 0)


class AccessControlTests(ClaimTestBase):
    def setUp(self):
        super().setUp()
        self.claim = make_claim(self.alice)

    def test_other_claimant_gets_404(self):
        self.client.force_login(self.bob)
        self.assertEqual(self.client.get(reverse("claims:detail", args=[self.claim.pk])).status_code, 404)
        self.assertEqual(self.client.get(f"/api/claims/{self.claim.pk}/").status_code, 404)

    def test_owner_agent_admin_can_view(self):
        for u in (self.alice, self.agent, self.admin):
            self.client.force_login(u)
            r = self.client.get(reverse("claims:detail", args=[self.claim.pk]))
            self.assertEqual(r.status_code, 200, u.username)

    def test_document_download_is_permission_checked(self):
        f = SimpleUploadedFile("photo.png", b"\x89PNG fake", content_type="image/png")
        self.client.force_login(self.alice)
        self.client.post(reverse("claims:add_documents", args=[self.claim.pk]), {"documents": [f]})
        doc = self.claim.documents.get()
        self.assertEqual(self.client.get(reverse("claims:document", args=[doc.pk])).status_code, 200)
        self.client.force_login(self.bob)
        self.assertEqual(self.client.get(reverse("claims:document", args=[doc.pk])).status_code, 404)

    def test_claimant_cannot_update_status(self):
        self.client.force_login(self.alice)
        r = self.client.post(reverse("claims:update_status", args=[self.claim.pk]), {"status": "approved"})
        self.assertEqual(r.status_code, 403)
        self.claim.refresh_from_db()
        self.assertEqual(self.claim.status, Claim.Status.RECEIVED)

    def test_admin_panel_restricted(self):
        for u, code in ((self.alice, 403), (self.agent, 403), (self.admin, 200)):
            self.client.force_login(u)
            self.assertEqual(self.client.get(reverse("claims:panel_home")).status_code, code, u.username)
            self.assertEqual(self.client.get(reverse("claims:panel_users")).status_code, code, u.username)


class StatusWorkflowTests(ClaimTestBase):
    def setUp(self):
        super().setUp()
        self.claim = make_claim(self.alice)

    def test_agent_updates_status_notifies_and_records_history(self):
        self.client.force_login(self.agent)
        r = self.client.post(
            reverse("claims:update_status", args=[self.claim.pk]),
            {"status": "under_review", "note": "Reviewing your photos."},
        )
        self.assertEqual(r.status_code, 302)
        self.claim.refresh_from_db()
        self.assertEqual(self.claim.status, Claim.Status.UNDER_REVIEW)
        h = self.claim.history.get()
        self.assertEqual((h.old_status, h.new_status, h.changed_by), ("received", "under_review", self.agent))
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("Under Review", mail.outbox[0].subject)
        self.assertEqual(mail.outbox[0].to, ["alice@example.com"])

    def test_unchanged_status_sends_nothing(self):
        self.client.force_login(self.agent)
        self.client.post(reverse("claims:update_status", args=[self.claim.pk]), {"status": "received"})
        self.assertEqual(len(mail.outbox), 0)
        self.assertEqual(self.claim.history.count(), 0)

    def test_only_admin_can_assign_agent(self):
        url = reverse("claims:update_status", args=[self.claim.pk])
        self.client.force_login(self.agent)
        self.client.post(url, {"status": "received", "assigned_agent": self.agent.pk})
        self.claim.refresh_from_db()
        self.assertIsNone(self.claim.assigned_agent)
        self.client.force_login(self.admin)
        self.client.post(url, {"status": "received", "assigned_agent": self.agent.pk})
        self.claim.refresh_from_db()
        self.assertEqual(self.claim.assigned_agent, self.agent)

    def test_invalid_status_raises(self):
        with self.assertRaises(ValueError):
            self.claim.change_status("bogus", self.agent)

    def test_message_notifies_other_party(self):
        self.claim.assigned_agent = self.agent
        self.claim.save()
        self.client.force_login(self.alice)
        self.client.post(reverse("claims:message", args=[self.claim.pk]), {"body": "Any update?"})
        self.assertEqual(self.claim.messages.count(), 1)
        self.assertEqual(mail.outbox[-1].to, ["agent1@example.com"])


class DashboardAndApiTests(ClaimTestBase):
    def setUp(self):
        super().setUp()
        self.c1 = make_claim(self.alice, incident_date=date(2026, 1, 10))
        self.c2 = make_claim(self.alice, incident_date=date(2026, 6, 10), status=Claim.Status.APPROVED)
        self.c3 = make_claim(self.bob)

    def _numbers(self, resp):
        return {c.claim_number for c in resp.context["page"]}

    def test_dashboard_shows_only_own_claims(self):
        self.client.force_login(self.alice)
        r = self.client.get(reverse("claims:dashboard"))
        self.assertEqual(self._numbers(r), {self.c1.claim_number, self.c2.claim_number})

    def test_filter_by_status_and_date(self):
        self.client.force_login(self.alice)
        r = self.client.get(reverse("claims:dashboard"), {"status": "approved"})
        self.assertEqual(self._numbers(r), {self.c2.claim_number})
        r = self.client.get(reverse("claims:dashboard"), {"from": "2026-01-01", "to": "2026-03-01"})
        self.assertEqual(self._numbers(r), {self.c1.claim_number})

    def test_admin_sees_all(self):
        self.client.force_login(self.admin)
        r = self.client.get(reverse("claims:dashboard"))
        self.assertEqual(len(self._numbers(r)), 3)

    def test_api_list_and_status_endpoint(self):
        self.client.force_login(self.alice)
        data = self.client.get("/api/claims/").json()
        self.assertEqual(data["count"], 2)
        s = self.client.get(f"/api/claims/{self.c2.pk}/status/").json()
        self.assertEqual(s["status"], "approved")

    def test_api_requires_auth_and_is_read_only(self):
        self.assertIn(self.client.get("/api/claims/").status_code, (401, 403))
        self.client.force_login(self.alice)
        r = self.client.post("/api/claims/", {})
        self.assertEqual(r.status_code, 405)


class AdminUserManagementTests(ClaimTestBase):
    def test_admin_can_promote_user_to_agent(self):
        self.client.force_login(self.admin)
        r = self.client.post(
            reverse("claims:panel_user_edit", args=[self.bob.pk]),
            {"role": "agent", "is_active": "on"},
        )
        self.assertEqual(r.status_code, 302)
        self.bob.refresh_from_db()
        self.assertEqual(self.bob.role, User.Role.AGENT)

    def test_admin_cannot_demote_self(self):
        self.client.force_login(self.admin)
        self.client.post(
            reverse("claims:panel_user_edit", args=[self.admin.pk]),
            {"role": "claimant", "is_active": "on"},
        )
        self.admin.refresh_from_db()
        self.assertEqual(self.admin.role, User.Role.ADMIN)
