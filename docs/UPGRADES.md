# Upgrade list (free now -> paid API later)

The AI feature is built with **free tools first**. Each row says what to swap and where.

| # | Area | Free now | Upgrade to (API) | Where to change |
|---|------|----------|------------------|-----------------|
| 1 | OCR of scans/photos | Tesseract + Poppler (local) | Vision LLM or cloud OCR (Claude vision, Google Vision, AWS Textract) | `claims/ai/ocr.py`: add a backend function, set `OCR_BACKEND` |
| 2 | Field extraction (step 3) | Rules / regex on OCR text | LLM returning JSON in the `ExtractedClaimData` schema | `claims/ai/` (step 3) |
| 3 | Damage analysis (step 4) | Simple image checks | Vision LLM describing damage + severity | `claims/ai/` (step 4) |
| 4 | Hindi / regional forms | `OCR_LANGUAGES=eng` | `eng+hin` pack, or a cloud OCR with Indic support | `.env` / settings |
| 5 | Background processing | Runs when you call it | Celery + Redis queue | `claims/ai/ingestion.py` |
| 6 | Uploaded files on Heroku/containers | Local disk (lost on restart) | S3 via `django-storages` | `STORAGES` in settings |
| 7 | Notifications at scale | Sent during the request | Background queue | `claims/notifications.py` |
| 8 | Live tracking | Polling every 10 s | WebSockets (Django Channels) | `claims/api.py` |
| 9 | Password reset | Missing | Django's built-in reset views + email | `accounts/` |
| 10 | Malware scan on uploads | None | ClamAV or a scanning API | `claims/forms.py` |

Keep this file updated: add a row whenever a step is built with a shortcut.
