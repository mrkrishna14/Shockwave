"""Invitation notification email. Messages notify only; they never carry an acceptance link or token."""
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import make_msgid

from shared.schemas import DeliveryResult


def invitation_message(notice):
    subject = f"{notice['provider_name']} invited you to {notice['api_name']} on Shockwave"
    body = (f"{notice['provider_name']} invited {notice['recipient_email']} to enroll in {notice['api_name']} on Shockwave.\n\n"
            f"To review the invitation, sign in to Shockwave (or create a consumer account) with this email address "
            f"at {notice['app_url']} and open Invitations.\n\n"
            "This email is a notification only. Invitations can only be accepted from inside your Shockwave workspace.\n"
            "If you were not expecting it, you can ignore this message.\n")
    return subject, body


class SMTPMailer:
    def __init__(self, settings):
        self.settings = settings

    def send_invitation(self, recipient_email, notice, idempotency_key):
        s = self.settings
        subject, body = invitation_message(notice)
        message = EmailMessage()
        message['Subject'] = subject
        message['From'] = s.smtp_from
        message['To'] = recipient_email
        message['Message-ID'] = make_msgid(domain=s.smtp_from.rsplit('@', 1)[-1].strip('> '))
        message['X-Shockwave-Operation'] = idempotency_key
        message.set_content(body)
        context = ssl.create_default_context()
        if s.smtp_port == 465:
            server = smtplib.SMTP_SSL(s.smtp_host, s.smtp_port, timeout=20, context=context)
        else:
            server = smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=20)
        with server:
            if s.smtp_port != 465 and s.smtp_starttls: server.starttls(context=context)
            if s.smtp_username: server.login(s.smtp_username, s.smtp_password)
            server.send_message(message)
        return DeliveryResult(status='sent', message_id=message['Message-ID'], error=None)
