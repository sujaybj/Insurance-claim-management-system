# ClaimDesk — Insurance Claim Management System

A Django web app for submitting, tracking and managing insurance claims, with role-based access for **claimants**, **insurance agents** and **admins**.

## Features
| Requirement | Implementation |
|---|---|
| Auth + roles | Custom `User` model with `role`; registration always creates claimants; admins promote users in the Admin panel |
| Claim submission | Validated form (policy format, no future dates, min description length, amount ≥ 1) + multi-file upload (JPG/PNG/PDF, 5 MB each, max 10) |
| Tracking | Status timeline, full status history, auto-refresh via polling the status API every 10 s |
| Notifications | Email on submission/status change/new message (SMTP); SMS via optional Twilio (logs only if not configured) |
| Dashboard | Claim history, counts per status, filters (status, incident date range, search), pagination |
| Admin panel | `/panel/`: stats, user/role management, assign agents; staff update statuses and message claimants from the claim page |
| Database | Relational schema (see `docs/DATABASE.md`), FK integrity, indexes |
| Security | CSRF, hashed passwords + validators, object-level access control (404 on others' claims), uploads served only through a permission-checked view, extension/size validation, HTTPS/HSTS/secure cookies when `DEBUG=0` |
| Tests | 26 unit/integration tests (`python manage.py test`) |
| Deployment | `Procfile` (Heroku), `Dockerfile` (AWS ECS/App Runner/any), WhiteNoise for static files |

## Stack
Python 3.13 · Django 6 · Django REST Framework · SQLite (dev) / PostgreSQL (prod) · Gunicorn · WhiteNoise.
(Chosen over MERN so that auth, ORM, migrations, admin and testing come built-in and security defaults are strong.)

## Local setup
```bash
python -m venv venv && source venv/bin/activate     # Windows: venv\Scripts\activate
pip install -r requirements.txt
python manage.py migrate
python manage.py seed_demo          # optional demo users + claims
python manage.py createsuperuser    # optional
python manage.py runserver
```
Open http://127.0.0.1:8000. Emails print to the terminal in dev.

Demo logins (after `seed_demo`, password `Demo@12345`): `claimant_demo`, `agent_demo`, `admin_demo`.
**Never run `seed_demo` in production.**

## Usage guide
- **Claimant**: Register → *New claim* → fill form, attach files → watch status on the claim page; reply to staff in *Messages*; add documents later.
- **Agent**: Sees unassigned + own assigned claims; updates status with a note (claimant is emailed/SMS'd).
- **Admin**: Everything above + assign agents, `/panel/users/` to change roles or deactivate accounts.
- To make the first admin: `python manage.py createsuperuser` (superusers have admin rights).

## Configuration (environment variables)
See `.env.example`. Key ones: `DEBUG`, `SECRET_KEY`, `ALLOWED_HOSTS`, `DATABASE_URL`, `EMAIL_*`, `TWILIO_*`, `MEDIA_ROOT`.

## Deployment
**Heroku**
```bash
heroku create && heroku addons:create heroku-postgresql
heroku config:set DEBUG=0 SECRET_KEY=$(python -c "import secrets;print(secrets.token_urlsafe(50))") \
  ALLOWED_HOSTS=<app>.herokuapp.com CSRF_TRUSTED_ORIGINS=https://<app>.herokuapp.com
git push heroku main
heroku run python manage.py createsuperuser
```
**AWS (container)**: build the `Dockerfile`, push to ECR, run on App Runner/ECS Fargate behind an ALB, RDS PostgreSQL for `DATABASE_URL`, SES for email.

> **Important:** Heroku/containers have ephemeral disks. Uploaded documents will be lost on restart unless you use persistent storage — switch `STORAGES["default"]` to S3 via `django-storages` before real use.

**Scalability**: stateless web tier (sessions in DB; use Redis cache/sessions if scaling out), run multiple Gunicorn workers/instances, Postgres with `conn_max_age`, indexes on `claimant,status`. Email/SMS are sent synchronously with `fail_silently`; for high volume move them to Celery.

## Testing
```bash
python manage.py test
```
Covers registration/roles, form + file validation, access control (cross-user 404s, 403s), status workflow + notifications, dashboard filters, API and user management. UAT script: `docs/UAT.md`.

## Docs
`docs/API.md` · `docs/DATABASE.md` · `docs/UAT.md` · `docs/RESUME.md`

## Known limitations / next steps
SMS needs Twilio credentials; polling instead of WebSockets (Django Channels is the upgrade); no malware scanning of uploads; no password-reset email flow yet; status transitions are unrestricted for staff.
