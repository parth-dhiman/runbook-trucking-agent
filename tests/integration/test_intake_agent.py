"""Small live test of the intake agent: one email per tool path, real Claude calls.

Covers every tool with 4 Claude calls total:
  email_001  missing PO       -> request_missing_info
  email_002  low quote        -> approve_quote -> raise_quote_for_approval (posts to Slack)
  email_003  complete order   -> create_load (saved to a throwaway Mongo database)

Emails are handed straight to the agent (no Gmail inbox), and replies to the
shipper are printed instead of sent. Stops at the first unexpected result to
save credits, and prints the token cost of every call.

Run from the project root (uses Anthropic credits):
    python -m tests.integration.test_intake_agent --live
"""

import datetime as dt
import json
import sys
from pathlib import Path
from unittest.mock import patch

from dotenv import load_dotenv

from src.agents.intake import agent, tools
from src.config.accounts import SHIPPER_EMAILS, TRUCKING_EMAIL
from src.services import claude, db

ROOT = Path(__file__).resolve().parents[2]
TEST_DB = "runbook_trucking_test"
# Claude Opus 5.5, USD per million tokens.
PRICE = {"input": 4.00, "output": 20.00, "cache_write": 5.00, "cache_read": 0.20}

CASES = [
    ("email_001", ["request_missing_info"], lambda steps: steps[-1]["result"]["missing_fields"] == ["customer_reference"]),
    ("email_002", ["approve_quote", "raise_quote_for_approval"], lambda steps: steps[0]["result"]["approved"] is False),
    ("email_003", ["create_load"], lambda steps: steps[-1]["result"]["price"] == 2500),
]


def _as_inbox_email(e: dict) -> dict:
    """Shape a data/emails.json entry like gmail.read_recent_emails output."""
    return {
        "id": f"test-{e['id']}",
        "thread_id": f"test-thread-{e['id']}",
        "from": f"{e['from_name']} <{SHIPPER_EMAILS[e['company']]}>",
        "to": TRUCKING_EMAIL,
        "subject": e["subject"],
        "received_at": dt.datetime.now(dt.timezone.utc),
        "body": e["body"],
        "attachments": [],
    }


def _cost(usage) -> float:
    return (
        usage.input_tokens * PRICE["input"]
        + usage.output_tokens * PRICE["output"]
        + (usage.cache_creation_input_tokens or 0) * PRICE["cache_write"]
        + (usage.cache_read_input_tokens or 0) * PRICE["cache_read"]
    ) / 1_000_000


def run() -> bool:
    load_dotenv()
    emails = {e["id"]: e for e in json.loads((ROOT / "data" / "emails.json").read_text(encoding="utf-8"))}
    real_send = claude.send
    calls, replies = [], []

    def tracked_send(*args, **kwargs):
        response = real_send(*args, **kwargs)
        u = response.usage
        calls.append(_cost(u))
        print(
            f"    [Claude call {len(calls)}: {u.input_tokens} in, {u.output_tokens} out, "
            f"cache write {u.cache_creation_input_tokens or 0}, cache read {u.cache_read_input_tokens or 0} "
            f"-> ${calls[-1]:.4f}]"
        )
        return response

    def capture_reply(sender, message_id, body, attachments=None):
        replies.append(body)
        return {"id": f"reply-to-{message_id}"}

    passed = True
    with patch.object(db, "DB_NAME", TEST_DB), patch.object(claude, "send", side_effect=tracked_send), \
         patch.object(tools.gmail, "reply_to_email", side_effect=capture_reply):
        try:
            for email_id, expected_tools, check in CASES:
                email = _as_inbox_email(emails[email_id])
                print(f"\n=== {email_id}: {email['subject']}")
                replies.clear()
                outcome = agent.handle_email(email)

                for step in outcome["steps"]:
                    print(f"  -> {step['tool']}{' (error)' if step['is_error'] else ''}: {step['result']}")
                if outcome["note"]:
                    print(f"  Claude: {outcome['note']}")
                for body in replies:
                    print("  Reply that would be sent:\n    " + body.replace("\n", "\n    "))

                done = [s for s in outcome["steps"] if not s["is_error"]]
                ok = outcome["finished"] and [s["tool"] for s in done] == expected_tools and check(done)
                print(f"  {'PASS' if ok else 'FAIL'} (expected {' -> '.join(expected_tools)})")
                if not ok:
                    passed = False
                    print("  Stopping early to save credits.")
                    break

            stored = list(db.get_db().loads.find({}, {"_id": 0, "load_id": 1, "status": 1, "price": 1}))
            print(f"\nLoads saved in Mongo test database: {stored}")
        finally:
            db.get_db().client.drop_database(TEST_DB)

    print(f"\n{'ALL PASSED' if passed else 'FAILED'} | {len(calls)} Claude calls | total cost ${sum(calls):.4f}")
    return passed


if __name__ == "__main__":
    if "--live" not in sys.argv:
        sys.exit("This test makes real Claude calls (about $0.10-0.20). Re-run with --live to go ahead.")
    sys.exit(0 if run() else 1)
