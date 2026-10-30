"""Integration check: send the order email that has a PDF attachment from a random shipper
account to the trucking inbox, then confirm read_recent_emails picks it up with the PDF intact.

Makes no Claude calls. Run from the project root: python -m tests.integration.test_gmail
"""

import datetime as dt
import json
import random
import time
from pathlib import Path

from src.config.accounts import SHIPPER_EMAILS, TRUCKING_EMAIL
from src.services import gmail

ROOT = Path(__file__).resolve().parents[2]
POLL_SECONDS = 5
TIMEOUT_SECONDS = 120


def test_send_and_read() -> dict:
    emails = json.loads((ROOT / "data" / "emails.json").read_text(encoding="utf-8"))
    email = next(e for e in emails if e.get("attachment"))
    pdf = ROOT / email["attachment"]
    sender = random.choice(list(SHIPPER_EMAILS.values()))
    # A unique subject so we can find this exact message in the inbox.
    subject = f"{email['subject']} [test {dt.datetime.now():%H:%M:%S}]"

    sent_at = time.time()
    sent = gmail.send_email(sender, TRUCKING_EMAIL, subject, email["body"], attachments=[pdf])

    received = None
    while time.time() - sent_at < TIMEOUT_SECONDS:
        recent = gmail.read_recent_emails(TRUCKING_EMAIL, time.time() - sent_at + 30)
        received = next((m for m in recent if m["subject"] == subject), None)
        if received:
            break
        time.sleep(POLL_SECONDS)
    assert received, f"Email not found in {TRUCKING_EMAIL} after {TIMEOUT_SECONDS}s"

    attachments = received["attachments"]
    assert [a["filename"] for a in attachments] == [pdf.name], attachments
    data = gmail.get_attachment(TRUCKING_EMAIL, received["id"], attachments[0]["attachment_id"])
    assert data == pdf.read_bytes(), "Downloaded attachment doesn't match the original PDF"

    return {
        "sender": sender,
        "sent_id": sent["id"],
        "received": received,
        "found_after_seconds": round(time.time() - sent_at, 1),
        "attachment_bytes": len(data),
    }


if __name__ == "__main__":
    result = test_send_and_read()
    received = result["received"]
    print(f"Sent from:      {result['sender']} (message {result['sent_id']})")
    print(f"Found in inbox: {TRUCKING_EMAIL} after {result['found_after_seconds']}s (message {received['id']})")
    print(f"  From:         {received['from']}")
    print(f"  Subject:      {received['subject']}")
    print(f"  Received at:  {received['received_at'].astimezone():%Y-%m-%d %H:%M:%S}")
    print(f"  Body starts:  {received['body'].splitlines()[0]!r} ({len(received['body'])} chars)")
    for a in received["attachments"]:
        print(f"  Attachment:   {a['filename']} ({a['mime_type']}, {a['size']:,} bytes)")
    print(f"  Downloaded {result['attachment_bytes']:,} bytes; matches the original PDF.")
