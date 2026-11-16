# User Acceptance Testing Plan

Use 3–5 testers per role (friends/classmates). Seed with `python manage.py seed_demo` or deploy a staging copy.

| # | Role | Scenario | Expected | Pass? |
|---|---|---|---|---|
| 1 | Claimant | Register with new email | Logged in, empty dashboard | |
| 2 | Claimant | Submit claim with 2 photos + 1 PDF | Claim `Received`, confirmation email | |
| 3 | Claimant | Submit with future date / short description / .exe file | Clear inline errors, nothing saved | |
| 4 | Claimant | Filter dashboard by status and date | Only matching claims shown | |
| 5 | Agent | Move claim to *Under Review* with note | Claimant gets email; timeline updates; claimant's open page refreshes within ~10 s | |
| 6 | Claimant | Reply to agent in Messages | Agent gets email | |
| 7 | Admin | Promote a user to Agent; assign them a claim | Agent sees claim in queue | |
| 8 | Claimant B | Open claimant A's claim URL | 404 | |
| 9 | Any | Use on phone | Layout usable | |

## Feedback form (ask each tester)
1. Was anything confusing? Where did you get stuck? 2. How long did submitting a claim take? 3. Were notifications clear/timely? 4. What's missing? 5. Rate ease of use 1–5.

Record results, then list changes made. (Put these in your README/resume — "Improved X after UAT feedback" is a strong talking point.)
