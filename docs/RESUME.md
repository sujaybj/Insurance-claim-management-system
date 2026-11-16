# Resume Section

**ClaimDesk — Insurance Claim Management System** | Python, Django, DRF, PostgreSQL, Docker, Heroku/AWS | [GitHub link] · [Live demo]
- Built a full-stack web app letting claimants submit/track insurance claims, with role-based access (claimant, agent, admin) and an admin panel for user and claim management.
- Designed a relational schema (users, claims, documents, status history, messages) with FK constraints, indexing and an audit trail of every status change.
- Implemented validated multi-file uploads (type/size checks) and permission-checked file serving; enforced object-level access control so users can only see their own claims.
- Added email/SMS notifications on status changes and a REST status API powering live claim tracking.
- Wrote 26 automated tests (access control, validation, workflow, API); containerised with Docker and deployed on <Heroku/AWS> with PostgreSQL.

**Challenges & how I solved them (for interviews):**
- *Data privacy*: users could guess claim/document IDs → scoped every query through one `claims_visible_to()` helper and return 404 for non-owners; tests assert it.
- *Privilege escalation*: public signup must never create admins → role is not a form field; promotion only via admin panel; admins can't demote themselves.
- *Unsafe uploads*: whitelisted extensions, size limits, random filenames, served via authenticated view instead of public media URL.
- *Notification reliability*: email/SMS failures must not roll back a status update → `fail_silently`/try-except; documented Celery as the scale-up path.
- *Ephemeral cloud disks*: identified that container storage loses uploads → documented S3 migration.
