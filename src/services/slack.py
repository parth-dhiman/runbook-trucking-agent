"""Slack service: post messages to a channel as the app's bot (chat.postMessage)."""

import json
import os
import urllib.request

POST_MESSAGE_URL = "https://slack.com/api/chat.postMessage"


def send_message(text: str, blocks: list[dict] | None = None) -> dict:
    """Post a message to SLACK_CHANNEL_ID using SLACK_BOT_TOKEN.

    `text` is the plain fallback shown in notifications; `blocks` is the
    optional Block Kit layout shown in the channel. Returns Slack's response.
    """
    payload = {"channel": os.environ["SLACK_CHANNEL_ID"], "text": text}
    if blocks:
        payload["blocks"] = blocks
    request = urllib.request.Request(
        POST_MESSAGE_URL,
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": f"Bearer {os.environ['SLACK_BOT_TOKEN']}",
        },
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        result = json.loads(response.read())
    # Slack reports API errors (missing scope, bot not in channel, ...) as HTTP 200 with ok=false.
    if not result.get("ok"):
        raise RuntimeError(f"Slack error: {result.get('error')}")
    return result
