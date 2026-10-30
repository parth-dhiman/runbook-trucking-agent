"""Inbox orchestrator: poll the trucking company inbox and hand each new email to the intake agent."""

import time

from google.auth.exceptions import TransportError
from googleapiclient.errors import HttpError

from src.agents.intake.agent import handle_email
from src.config.accounts import TRUCKING_EMAIL
from src.config.params import LOOKBACK_SECONDS, POLL_INTERVAL_SECONDS
from src.services.gmail import read_recent_emails


def _summary(result) -> str:
    if not isinstance(result, dict):
        # Tool errors can span several lines (e.g. validation errors); keep the console to one line.
        lines = (line.strip() for line in str(result).splitlines())
        return " ".join(line for line in lines if line and "errors.pydantic.dev" not in line)
    return ", ".join(f"{key}={value}" for key, value in result.items())


def _process(email: dict) -> None:
    received = email["received_at"].astimezone().strftime("%H:%M:%S")
    print(f"\n[{received}] New email from {email['from']}")
    print(f"  Subject: {email['subject']}")
    if email["attachments"]:
        print(f"  Attachments: {', '.join(a['filename'] for a in email['attachments'])}")

    try:
        outcome = handle_email(email)
    except Exception as err:  # one bad email shouldn't stop the inbox watcher
        print(f"  ! Could not process: {type(err).__name__}: {err}")
        return

    if not outcome["steps"]:
        print(f"  -> No action: {outcome['note'] or 'not an order request'}")
    for step in outcome["steps"]:
        print(f"  -> {step['tool']}{' (error)' if step['is_error'] else ''}: {_summary(step['result'])}")
    if not outcome["finished"]:
        print("  ! Stopped before finishing; check this email by hand.")


def main() -> None:
    print(f"Watching {TRUCKING_EMAIL} every {POLL_INTERVAL_SECONDS}s. Press Ctrl+C to stop.")
    seen: dict[str, float] = {}
    last_poll = time.time()
    try:
        while True:
            started = time.monotonic()
            poll_time = time.time()
            try:
                # Cover everything since the previous check (processing can take a while), plus overlap.
                emails = read_recent_emails(TRUCKING_EMAIL, poll_time - last_poll + LOOKBACK_SECONDS)
                last_poll = poll_time
            except (HttpError, TransportError, OSError) as err:
                print(f"Poll failed, will retry: {err}")
                emails = []

            for email in emails:
                if email["id"] in seen:
                    continue
                seen[email["id"]] = email["received_at"].timestamp()
                if TRUCKING_EMAIL in email["from"].lower():
                    continue  # our own messages
                _process(email)

            cutoff = last_poll - LOOKBACK_SECONDS
            seen = {msg_id: ts for msg_id, ts in seen.items() if ts >= cutoff}
            time.sleep(max(0.0, POLL_INTERVAL_SECONDS - (time.monotonic() - started)))
    except KeyboardInterrupt:
        print("\nStopped.")
