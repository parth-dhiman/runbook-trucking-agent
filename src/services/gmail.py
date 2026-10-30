"""Gmail service: per-account OAuth tokens, sending, and replying."""

import base64
import mimetypes
import re
import time
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

ROOT = Path(__file__).resolve().parents[2]
CREDS_PATH = ROOT / "secrets" / "gmail" / "creds.json"
TOKEN_DIR = ROOT / "secrets" / "gmail" / "token"

# Read, send, and label mail (needed later for reading order emails).
SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]


def token_path(email: str) -> Path:
    return TOKEN_DIR / f"{email.strip().lower()}.json"


def create_token(email: str) -> Credentials:
    """Run the browser consent flow for `email` and save its token.

    Raises ValueError if the user signs in with a different account.
    """
    flow = InstalledAppFlow.from_client_secrets_file(str(CREDS_PATH), SCOPES)
    creds = flow.run_local_server(port=0, login_hint=email, prompt="consent")

    profile = build("gmail", "v1", credentials=creds, cache_discovery=False).users().getProfile(userId="me").execute()
    signed_in_as = profile["emailAddress"]
    if signed_in_as.lower() != email.strip().lower():
        raise ValueError(f"Signed in as {signed_in_as}, expected {email}. Token not saved.")

    TOKEN_DIR.mkdir(parents=True, exist_ok=True)
    token_path(email).write_text(creds.to_json())
    return creds


def get_credentials(email: str) -> Credentials:
    """Load the saved token for `email`, refreshing or re-creating it as needed."""
    path = token_path(email)
    if not path.exists():
        return create_token(email)

    creds = Credentials.from_authorized_user_file(str(path), SCOPES)
    if creds.valid:
        return creds
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except RefreshError:
            return create_token(email)
        path.write_text(creds.to_json())
        return creds
    return create_token(email)


def get_service(email: str):
    return build("gmail", "v1", credentials=get_credentials(email), cache_discovery=False)


def _build_message(
    sender: str,
    to: str | list[str],
    subject: str,
    body: str,
    cc: str | list[str] | None = None,
    attachments: list[str | Path] | None = None,
) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to if isinstance(to, str) else ", ".join(to)
    if cc:
        msg["Cc"] = cc if isinstance(cc, str) else ", ".join(cc)
    msg["Subject"] = subject
    msg.set_content(body)

    for attachment in attachments or []:
        path = Path(attachment)
        mime_type, _ = mimetypes.guess_type(path.name)
        maintype, subtype = (mime_type or "application/octet-stream").split("/", 1)
        msg.add_attachment(path.read_bytes(), maintype=maintype, subtype=subtype, filename=path.name)
    return msg


def _send(service, msg: EmailMessage, thread_id: str | None = None) -> dict:
    payload = {"raw": base64.urlsafe_b64encode(msg.as_bytes()).decode()}
    if thread_id:
        payload["threadId"] = thread_id
    return service.users().messages().send(userId="me", body=payload).execute()


def send_email(
    sender: str,
    to: str | list[str],
    subject: str,
    body: str,
    cc: str | list[str] | None = None,
    attachments: list[str | Path] | None = None,
) -> dict:
    """Send a new email from `sender`. Returns the Gmail message (id, threadId)."""
    msg = _build_message(sender, to, subject, body, cc, attachments)
    return _send(get_service(sender), msg)


def reply_to_email(
    sender: str,
    message_id: str,
    body: str,
    attachments: list[str | Path] | None = None,
) -> dict:
    """Reply from `sender` to the Gmail message `message_id`, in the same thread."""
    service = get_service(sender)
    original = service.users().messages().get(userId="me", id=message_id, format="metadata").execute()
    headers = {h["name"].lower(): h["value"] for h in original["payload"]["headers"]}

    subject = headers.get("subject", "")
    if not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"

    msg = _build_message(sender, headers.get("reply-to") or headers["from"], subject, body, attachments=attachments)
    rfc_message_id = headers.get("message-id")
    if rfc_message_id:
        msg["In-Reply-To"] = rfc_message_id
        references = headers.get("references")
        msg["References"] = f"{references} {rfc_message_id}" if references else rfc_message_id

    return _send(service, msg, original["threadId"])


def _walk_parts(part: dict):
    yield part
    for child in part.get("parts", []):
        yield from _walk_parts(child)


def _decode_body(part: dict) -> str:
    data = part.get("body", {}).get("data", "")
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", errors="replace")


def _parse_message(raw: dict) -> dict:
    headers = {h["name"].lower(): h["value"] for h in raw["payload"].get("headers", [])}
    parts = list(_walk_parts(raw["payload"]))
    inline = [p for p in parts if not p.get("filename")]

    body = next((_decode_body(p) for p in inline if p.get("mimeType") == "text/plain"), "")
    if not body:
        html = next((_decode_body(p) for p in inline if p.get("mimeType") == "text/html"), "")
        body = re.sub(r"<[^>]+>", " ", html)

    return {
        "id": raw["id"],
        "thread_id": raw["threadId"],
        "from": headers.get("from", ""),
        "to": headers.get("to", ""),
        "subject": headers.get("subject", ""),
        "received_at": datetime.fromtimestamp(int(raw["internalDate"]) / 1000, tz=timezone.utc),
        "body": body.strip(),
        "attachments": [
            {
                "filename": p["filename"],
                "mime_type": p.get("mimeType", ""),
                "attachment_id": p["body"].get("attachmentId"),
                "size": p["body"].get("size", 0),
            }
            for p in parts
            if p.get("filename")
        ],
    }


def get_attachment(email: str, message_id: str, attachment_id: str) -> bytes:
    """Download one attachment (by the ids from read_recent_emails) as raw bytes."""
    attachment = get_service(email).users().messages().attachments().get(
        userId="me", messageId=message_id, id=attachment_id
    ).execute()
    data = attachment["data"]
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def read_recent_emails(email: str, seconds: float) -> list[dict]:
    """Return inbox messages `email` received in the last `seconds`, oldest first.

    Each message has id, thread_id, from, to, subject, received_at (UTC datetime),
    body (plain text), and attachments (filename, mime_type, attachment_id, size).
    """
    service = get_service(email)
    cutoff = time.time() - seconds

    ids, page_token = [], None
    while True:
        resp = service.users().messages().list(
            userId="me", q=f"in:inbox after:{int(cutoff)}", pageToken=page_token
        ).execute()
        ids += [m["id"] for m in resp.get("messages", [])]
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    messages = [_parse_message(service.users().messages().get(userId="me", id=i, format="full").execute()) for i in ids]
    # Gmail's after: filter is coarse, so trim to the exact window by received time.
    messages = [m for m in messages if m["received_at"].timestamp() >= cutoff]
    return sorted(messages, key=lambda m: m["received_at"])


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Create a Gmail token for an account.")
    parser.add_argument("email")
    args = parser.parse_args()
    create_token(args.email)
    print(f"Token saved to {token_path(args.email)}")
