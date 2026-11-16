"""Email + SMS notifications for claim events.

Email uses Django's configured backend (console in dev, SMTP in prod).
SMS is pluggable: by default it only logs; set TWILIO_* env vars and install
`twilio` to send real messages.
"""
import logging
import os

from django.conf import settings
from django.core.mail import send_mail

logger = logging.getLogger(__name__)


def send_sms(phone, text):
    sid = os.environ.get("TWILIO_ACCOUNT_SID")
    token = os.environ.get("TWILIO_AUTH_TOKEN")
    sender = os.environ.get("TWILIO_FROM")
    if not (phone and sid and token and sender):
        logger.info("SMS (not sent, Twilio not configured) to %s: %s", phone, text)
        return False
    try:
        from twilio.rest import Client  # optional dependency

        Client(sid, token).messages.create(to=phone, from_=sender, body=text)
        return True
    except Exception:  # never let SMS failure break a status update
        logger.exception("SMS send failed")
        return False


def notify_status_change(claim, note=""):
    user = claim.claimant
    status = claim.get_status_display()
    subject = f"Claim {claim.claim_number}: {status}"
    body = (
        f"Hello {user.first_name or user.username},\n\n"
        f"The status of your claim {claim.claim_number} is now: {status}.\n"
    )
    if note:
        body += f"\nNote from our team:\n{note}\n"
    body += "\nLog in to view full details.\n"
    send_mail(subject, body, settings.DEFAULT_FROM_EMAIL, [user.email], fail_silently=True)
    send_sms(user.phone, f"Claim {claim.claim_number} is now: {status}.")


def notify_new_message(claim, message):
    """Email the other party when a message is posted on a claim."""
    if message.author_id == claim.claimant_id:
        recipient = claim.assigned_agent
    else:
        recipient = claim.claimant
    if not recipient or not recipient.email:
        return
    send_mail(
        f"New message on claim {claim.claim_number}",
        f"{message.author.username} wrote:\n\n{message.body}\n\nLog in to reply.",
        settings.DEFAULT_FROM_EMAIL,
        [recipient.email],
        fail_silently=True,
    )
