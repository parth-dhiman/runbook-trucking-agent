"""Claude service: send one request (text, attachments, tools) and run the tools Claude calls."""

import base64
import json
import mimetypes
from pathlib import Path
from typing import Callable

import anthropic

MODEL = "claude-opus-5-5"
EFFORT = "medium"
MAX_TOKENS = 16000
# If Claude declines a request, the API re-runs it on Anthropic's recommended fallback model.
BETAS = ["server-side-fallback-2026-07-01"]

IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
TEXT_TYPES = {"application/json", "application/xml"}

# A file path, or (filename, raw bytes) for files already in memory, such as Gmail attachments.
Attachment = str | Path | tuple[str, bytes]

_client: anthropic.Anthropic | None = None


def get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from the environment
    return _client


def _attachment_block(attachment: Attachment) -> dict:
    if isinstance(attachment, tuple):
        filename, data = attachment
    else:
        path = Path(attachment)
        filename, data = path.name, path.read_bytes()
    mime_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"

    if mime_type == "application/pdf":
        source = {"type": "base64", "media_type": mime_type, "data": base64.standard_b64encode(data).decode()}
        return {"type": "document", "title": filename, "source": source}
    if mime_type in IMAGE_TYPES:
        source = {"type": "base64", "media_type": mime_type, "data": base64.standard_b64encode(data).decode()}
        return {"type": "image", "source": source}
    if mime_type.startswith("text/") or mime_type in TEXT_TYPES:
        source = {"type": "text", "media_type": "text/plain", "data": data.decode("utf-8", errors="replace")}
        return {"type": "document", "title": filename, "source": source}
    # Let Claude know the file exists even though it can't be read.
    return {"type": "text", "text": f"[Attachment '{filename}' ({mime_type}) could not be read.]"}


def user_content(prompt: str, attachments: list[Attachment] | None = None) -> list[dict]:
    """Content for a user message: attachments first, then the prompt text."""
    return [_attachment_block(a) for a in attachments or []] + [{"type": "text", "text": prompt}]


def send(
    messages: list[dict],
    system: str | None = None,
    tools: list[dict] | None = None,
    one_tool_at_a_time: bool = False,
    effort: str = EFFORT,
    max_tokens: int = MAX_TOKENS,
):
    """Send a conversation to Claude and return the response.

    `tools` are tool definitions ({name, description, input_schema}); any tool
    Claude calls comes back as a `tool_use` block in `response.content`, ready
    for `run_tool_calls`. With `one_tool_at_a_time`, Claude calls at most one
    tool per response, so each step can depend on the last one's result.
    """
    optional = {}
    if system:
        # Cache the system prompt; it's identical on every call.
        optional["system"] = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
    if tools:
        optional["tools"] = tools
        if one_tool_at_a_time:
            optional["tool_choice"] = {"type": "auto", "disable_parallel_tool_use": True}

    return get_client().beta.messages.create(
        model=MODEL,
        max_tokens=max_tokens,
        betas=BETAS,
        fallbacks="default",
        output_config={"effort": effort},
        messages=messages,
        **optional,
    )


def ask(
    prompt: str,
    system: str | None = None,
    attachments: list[Attachment] | None = None,
    tools: list[dict] | None = None,
    effort: str = EFFORT,
    max_tokens: int = MAX_TOKENS,
):
    """Send one user message (prompt plus attachments) to Claude and return the response."""
    messages = [{"role": "user", "content": user_content(prompt, attachments)}]
    return send(messages, system=system, tools=tools, effort=effort, max_tokens=max_tokens)


def response_text(response) -> str:
    return "".join(block.text for block in response.content if block.type == "text")


def tool_calls(response) -> list:
    return [block for block in response.content if block.type == "tool_use"]


def run_tool_calls(response, handlers: dict[str, Callable]) -> list[dict]:
    """Run each tool Claude called through the handler with the same name.

    Handlers are called with the tool input as keyword arguments. Returns one
    `tool_result` block per call (with is_error set on failure), which can be
    logged or sent back to Claude in a follow-up turn.
    """
    if response.stop_reason == "refusal":
        raise RuntimeError("Claude declined the request.")
    if response.stop_reason == "max_tokens":
        raise RuntimeError("Claude hit max_tokens, so tool input may be incomplete.")

    results = []
    for call in tool_calls(response):
        handler = handlers.get(call.name)
        if handler is None:
            results.append({"type": "tool_result", "tool_use_id": call.id, "content": f"Unknown tool: {call.name}", "is_error": True})
            continue
        try:
            output = handler(**call.input)
        except Exception as err:
            results.append({"type": "tool_result", "tool_use_id": call.id, "content": f"Error: {err}", "is_error": True})
            continue
        content = output if isinstance(output, str) else json.dumps(output, default=str)
        results.append({"type": "tool_result", "tool_use_id": call.id, "content": content})
    return results
