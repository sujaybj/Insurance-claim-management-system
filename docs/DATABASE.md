# Database Design

```
User (accounts_user)              Claim                              ClaimDocument
 id PK                             id PK                              id PK
 username UNIQUE                   claim_number UNIQUE                claim_id FK → Claim (CASCADE)
 email UNIQUE                      claimant_id FK → User (PROTECT)    file
 password (hashed)                 assigned_agent_id FK → User (SET NULL)  original_name
 role {claimant,agent,admin}       policy_number, claim_type          uploaded_at
 phone, is_active, …               incident_date, amount_claimed
                                   description, status (indexed)     StatusHistory
                                   created_at, updated_at             id PK, claim_id FK, old_status, new_status,
                                   INDEX(claimant, status)            changed_by_id FK (SET NULL), note, created_at

                                                                      ClaimMessage
                                                                       id PK, claim_id FK, author_id FK, body, created_at
```
**Integrity**: claimants can't be deleted while they own claims (`PROTECT`); deleting a claim cascades to its documents/history/messages; unique claim numbers and emails; amount validator ≥ 1; status limited to choices and changed via `Claim.change_status()` which always writes history.

**Security**: Argon/PBKDF2-hashed passwords, CSRF tokens, ORM-parameterised queries (no raw SQL), random upload filenames, uploads outside the static path and served via permission-checked view.
