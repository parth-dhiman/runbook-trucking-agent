"""Order intake agent: give Claude one inbox email and run the tools it chooses.

Flow for each email:
1. Build a message with the email's details and its attachments (PDFs etc.).
2. Send it to Claude with the intake skill (skills/intake.md) and the intake tools.
3. Run the tool Claude calls. If that tool finishes the email (create_load,
   request_missing_info, raise_quote_for_approval), stop. Otherwise (approve_quote)
   send the result back to Claude so it can take the next step.
"""

import json
from functools import partial
from pathlib import Path

from src.agents.intake import tools
from src.config.accounts import TRUCKING_EMAIL
from src.services import claude, gmail

ROOT = Path(__file__).resolve().parents[3]
SKILL = (ROOT / "skills" / "intake.md").read_text(encoding="utf-8")
TOOLS = [
    tools.CREATE_LOAD_TOOL,
    tools.REQUEST_MISSING_INFO_TOOL,
    tools.APPROVE_QUOTE_TOOL,
    tools.RAISE_QUOTE_FOR_APPROVAL_TOOL,
]
# Once one of these succeeds, the email is handled. approve_quote is a check, so its
# result goes back to Claude instead.
FINAL_TOOLS = {"create_load", "request_missing_info", "raise_quote_for_approval"}
MAX_TURNS = 4


def _prompt(email: dict) -> str:
    received = email["received_at"].astimezone()
    lines = [
        f"Received: {received:%A, %B %d, %Y at %H:%M}",
        f"From: {email['from']}",
        f"To: {email['to']}",
        f"Subject: {email['subject']}",
    ]
    if email["attachments"]:
        lines.append("Attachments: " + ", ".join(a["filename"] for a in email["attachments"]))
    return "\n".join(lines) + "\n\n" + email["body"]


def _attachments(email: dict) -> list[tuple[str, bytes]]:
    return [
        (a["filename"], gmail.get_attachment(TRUCKING_EMAIL, email["id"], a["attachment_id"]))
        for a in email["attachments"]
        if a["attachment_id"]
    ]


def _handlers(email: dict) -> dict:
    """Tool name -> function. Tools that reply to the shipper get the source email from us, not Claude."""
    source = {"message_id": email["id"], "thread_id": email["thread_id"], "from": email["from"], "subject": email["subject"]}
    return {
        "create_load": partial(tools.create_load, source=source),
        "request_missing_info": partial(tools.request_missing_info, source=source),
        "approve_quote": tools.approve_quote,
        "raise_quote_for_approval": partial(tools.raise_quote_for_approval, source=source),
    }


def _parse(content: str):
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        return content


def handle_email(email: dict) -> dict:
    """Let Claude decide what to do with one email (from gmail.read_recent_emails) and run its tools.

    Returns {"steps": [{"tool", "result", "is_error"}, ...], "note": Claude's text, "finished": bool}.
    No steps means Claude decided the email isn't an order request.
    """
    handlers = _handlers(email)
    messages = [{"role": "user", "content": claude.user_content(_prompt(email), _attachments(email))}]
    steps, note = [], ""

    for _ in range(MAX_TURNS):
        response = claude.send(messages, system=SKILL, tools=TOOLS, one_tool_at_a_time=True)
        if response.stop_reason in ("refusal", "max_tokens"):
            raise RuntimeError(f"Claude stopped early ({response.stop_reason}).")
        note = claude.response_text(response).strip() or note
        calls = claude.tool_calls(response)
        if not calls:
            return {"steps": steps, "note": note, "finished": True}

        results = claude.run_tool_calls(response, handlers)
        for call, result in zip(calls, results):
            steps.append({"tool": call.name, "result": _parse(result["content"]), "is_error": result.get("is_error", False)})
        if any(call.name in FINAL_TOOLS and not result.get("is_error") for call, result in zip(calls, results)):
            return {"steps": steps, "note": note, "finished": True}

        # Send the tool results (including any errors) back so Claude can continue or fix its input.
        messages += [{"role": "assistant", "content": response.content}, {"role": "user", "content": results}]

    return {"steps": steps, "note": note, "finished": False}
