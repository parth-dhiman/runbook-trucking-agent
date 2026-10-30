"""Integration check: post one test message to the Slack channel.

Run from the project root: python -m tests.integration.test_slack
"""

import datetime as dt

from dotenv import load_dotenv

from src.services import slack


def test_send_message() -> dict:
    load_dotenv()
    result = slack.send_message(f"Test message from the trucking intake agent ({dt.datetime.now():%Y-%m-%d %H:%M:%S})")
    assert result["ok"]
    return result


if __name__ == "__main__":
    result = test_send_message()
    print(f"ok={result['ok']} channel={result['channel']} ts={result['ts']}")
    print(f"posted text: {result['message']['text']}")
