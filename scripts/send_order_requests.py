"""Send the sample order emails from shipper accounts to the trucking company inbox.

Each run assigns the shipper accounts to the emails in a new random order.
Run from the project root: python -m scripts.send_order_requests
"""

import json
import random
import time
from pathlib import Path

from src.config.accounts import SHIPPER_EMAILS, TRUCKING_EMAIL
from src.services.gmail import send_email

ROOT = Path(__file__).resolve().parents[1]
EMAILS_PATH = ROOT / "data" / "emails.json"
SEND_INTERVAL_SECONDS = 5


def send_order_requests() -> list[dict]:
    emails = json.loads(EMAILS_PATH.read_text(encoding="utf-8"))
    senders = random.sample(list(SHIPPER_EMAILS.values()), len(SHIPPER_EMAILS))
    if len(senders) < len(emails):
        raise ValueError(f"{len(emails)} emails but only {len(senders)} shipper accounts.")

    sent = []
    for i, (email, sender) in enumerate(zip(emails, senders)):
        if i:
            time.sleep(SEND_INTERVAL_SECONDS)
        attachment = email.get("attachment")
        attachments = [ROOT / attachment] if attachment else None
        result = send_email(sender, TRUCKING_EMAIL, email["subject"], email["body"], attachments=attachments)
        print(f"{email['id']}  {sender:28} -> {TRUCKING_EMAIL}  | {email['subject']}" + (f"  [+ {attachment}]" if attachment else ""))
        sent.append({"email_id": email["id"], "sender": sender, "message_id": result["id"]})
    return sent


if __name__ == "__main__":
    send_order_requests()
