import shutil
import tempfile
from datetime import date, timedelta

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from .forms import ClaimForm, DocumentUploadForm
from .models import Claim
from .tests import VALID_FORM, make_claim, make_user

TMP = tempfile.mkdtemp()


def upload(name, content=b"x" * 100):
    return SimpleUploadedFile(name, content)


@override_settings(MEDIA_ROOT=TMP)
class MultiFileUploadTests(TestCase):
    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(TMP, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        self.user = make_user("multi")
        self.client.force_login(self.user)

    def test_new_claim_keeps_every_selected_file(self):
        files = [upload("form.png"), upload("front.jpg"), upload("rear.jpeg"), upload("report.pdf")]
        r = self.client.post(reverse("claims:create"), {**VALID_FORM, "documents": files})
        self.assertEqual(r.status_code, 302)
        claim = Claim.objects.get()
        self.assertEqual(sorted(claim.documents.values_list("original_name", flat=True)),
                         ["form.png", "front.jpg", "rear.jpeg", "report.pdf"])

    def test_upload_more_adds_two_files_to_an_existing_claim(self):
        claim = make_claim(self.user)
        self.client.post(reverse("claims:add_documents", args=[claim.pk]), {"documents": [upload("a.png")]})
        r = self.client.post(reverse("claims:add_documents", args=[claim.pk]),
                             {"documents": [upload("b.jpg"), upload("c.pdf")]}, follow=True)
        self.assertEqual(claim.documents.count(), 3)
        self.assertContains(r, "Documents uploaded.")

    def test_one_bad_file_rejects_the_batch_with_a_message_naming_it(self):
        claim = make_claim(self.user)
        r = self.client.post(reverse("claims:add_documents", args=[claim.pk]),
                             {"documents": [upload("ok.png"), upload("phone.heic")]}, follow=True)
        self.assertEqual(claim.documents.count(), 0)
        self.assertContains(r, "phone.heic")

    def test_file_pickers_only_offer_allowed_types(self):
        self.assertIn('accept=".jpg,.jpeg,.png,.pdf"', str(DocumentUploadForm()["documents"]))
        self.assertIn('accept=".jpg,.jpeg,.png,.pdf"', str(ClaimForm()["documents"]))
        self.assertIn("multiple", str(DocumentUploadForm()["documents"]))

    def test_detail_page_explains_the_upload_button(self):
        claim = make_claim(self.user)
        r = self.client.get(reverse("claims:detail", args=[claim.pk]))
        self.assertContains(r, "press the button below to upload them")
