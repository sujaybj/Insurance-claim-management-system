# API Documentation

Session-authenticated (log in via the site; send the `sessionid` cookie). Read-only. Paginated (20/page). Visibility follows role: claimants see own claims; agents see assigned + unassigned; admins see all. Claims you can't see return **404**.

## `GET /api/claims/`
Query params (all optional): `status` (`received|under_review|approved|denied`), `from`, `to` (incident date, `YYYY-MM-DD`), `page`.
```json
{"count":2,"next":null,"previous":null,"results":[{
 "id":1,"claim_number":"CLM-3F9A1B2C","claimant":"alice","policy_number":"POL-123456",
 "claim_type":"auto","incident_date":"2026-11-10","amount_claimed":"5000.00",
 "description":"…","status":"received","status_display":"Received",
 "created_at":"2026-11-13T10:00:00+05:30","updated_at":"2026-11-13T10:00:00+05:30"}]}
```
## `GET /api/claims/{id}/`
Single claim (same shape).

## `GET /api/claims/{id}/status/`
Lightweight status + history; used by the claim page for live updates.
```json
{"claim_number":"CLM-3F9A1B2C","status":"under_review","status_display":"Under Review",
 "updated_at":"…","history":[{"from":"received","to":"under_review","note":"Reviewing photos","at":"…"}]}
```
## Errors
`401/403` not authenticated · `404` not found / not permitted · `405` write methods (claims are created/updated via the web UI).

## Web routes (HTML)
| Route | Who | Purpose |
|---|---|---|
| `/accounts/register/`, `/accounts/login/`, `/accounts/logout/` | all | auth |
| `/` | logged in | dashboard + filters |
| `/claims/new/` | claimant | submit claim |
| `/claims/<id>/` | owner/staff | detail, timeline, messages |
| `/claims/<id>/status/` (POST) | agent/admin | update status, assign agent (admin) |
| `/documents/<id>/` | owner/staff | download upload |
| `/panel/`, `/panel/users/` | admin | admin panel |
