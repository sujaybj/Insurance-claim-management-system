# Upgrade list - Version 2 plan (free now -> better models later)

Version 1 of the AI feature is built with **free tools first** and finished completely before any of the rows below are started. Each row says what to swap and where.

| # | Area | Free now | Upgrade to (API) | Where to change |
|---|------|----------|------------------|-----------------|
| 1 | OCR of scans/photos | Tesseract + Poppler (local) | Vision LLM or cloud OCR (Claude vision, Google Vision, AWS Textract) | `claims/ai/ocr.py`: add a backend function, set `OCR_BACKEND` |
| 2 | Field extraction (step 3) | Rules / regex on OCR text | LLM returning JSON in the `ExtractedClaimData` schema | `claims/ai/` (step 3) |
| 3 | Damage analysis (step 4) | Photo quality checks (blur, light, size, duplicates) + severity guessed from the damage words and claimed amount. It cannot see damage in a photo | Vision LLM looks at the photos, describes the damage and rates severity | `claims/ai/damage.py`, `claims/ai/pipeline.py` |
| 4 | Hindi / regional forms | `OCR_LANGUAGES=eng` | `eng+hin` pack, or a cloud OCR with Indic support | `.env` / settings |
| 5 | Background processing | Runs when you call it | Celery + Redis queue | `claims/ai/ingestion.py` |
| 6 | Uploaded files on Heroku/containers | Local disk (lost on restart) | S3 via `django-storages` | `STORAGES` in settings |
| 7 | Notifications at scale | Sent during the request | Background queue | `claims/notifications.py` |
| 8 | Live tracking | Polling every 10 s | WebSockets (Django Channels) | `claims/api.py` |
| 9 | Password reset | Missing | Django's built-in reset views + email | `accounts/` |
| 10 | Malware scan on uploads | None | ClamAV or a scanning API | `claims/forms.py` |
| 11 | Name/policy/vehicle reading | Label rules + a built-in list of ~35 vehicle makes | LLM extraction reads any form layout, including unlabelled and handwritten ones | `claims/ai/extractor.py` (`extract_fields`) |
| 12 | Confidence score | Formula: completeness x OCR quality x photo quality | Model's own confidence plus a check against labelled past claims | `confidence_score` in `claims/ai/damage.py` |
| 13 | Looking at the photos (local model) | Not used | A local damage detector (e.g. a YOLO-style model) giving damaged parts and area, with severity rules. Check first: the model's licence (Ultralytics YOLO is AGPL-3.0 and needs a paid licence for closed-source/client deployments), PyTorch's size on Heroku (Docker may be needed), that the weights come from a trusted source, that the class names match the severity rules, and that "nothing detected" means *unknown*, not *minor* | `claims/ai/damage.py` (new detector next to the photo checks), `claims/ai/pipeline.py` |

Keep this file updated: add a row whenever a step is built with a shortcut.
