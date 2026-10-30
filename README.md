# Trucking Order Intake Agent

A demo agent that works a trucking company's inbox. Shippers email in load orders; Claude reads each email (including PDF attachments) and takes one of these actions:

| The email is... | What happens |
|---|---|
| Not an order | Nothing. |
| An order with missing details | Shipper gets a reply listing what's missing. |
| A complete order | Load is saved to MongoDB and the shipper gets a booking confirmation. |
| A complete order with a price that's too low | Team gets a Slack message to approve it, and the shipper gets a "we're reviewing your rate" reply. |

**Pricing is one rule:** each lane (pickup state → delivery state + equipment) has a standard price in `data/rates.json`. A quote at or above it is approved; below it goes to Slack.

**A complete order needs:** customer reference (PO/order #), pickup address and date, delivery address and date, commodity, weight in lbs, and equipment type (plus temperature for reefers). See `data/order_spec.json`.

---

## Setup

### 1. What you need

- Python 3.13
- MongoDB running locally on the default port (27017)
- An Anthropic API key
- A Slack app with a bot token (`chat:write` scope), added to your channel
- 6 Gmail accounts: 1 for the trucking company, 5 for the shippers
- A Google Cloud OAuth client (desktop app) with the Gmail API turned on, saved as `secrets/gmail/creds.json`

### 2. Install

```
python -m venv .venv
.venv\Scripts\activate          # Windows
source .venv/bin/activate       # Mac/Linux
pip install -r requirements.txt
```

### 3. Add your keys

Create a `.env` file in the project root:

```
ANTHROPIC_API_KEY=...
MONGO_USERNAME=...
MONGO_PASSWORD=...
SLACK_BOT_TOKEN=xoxb-...
SLACK_CHANNEL_ID=C...
```

To find the channel ID in Slack: right-click the channel → View channel details → it's at the bottom.

### 4. Set your Gmail accounts

Put the trucking account and the 5 shipper accounts in `src/config/accounts.py`.

### 5. Sign in to each Gmail account (once)

In `src/config/params.py`, set `MODE = "auth"`. Then run this once per account (6 times), entering the email and signing in when the browser opens:

```
python main.py
```

Tokens are saved in `secrets/gmail/token/`. If your Google Cloud app is in testing mode, add each account as a test user first.

---

## Tests

Run these from the project root, in this order. Each one checks one piece before you run the whole thing.

### 1. Slack (free)

```
python -m tests.integration.test_slack
```

Posts one test message to your channel. Passes if it prints `ok=True`. Confirms the bot token and channel are set up.

### 2. Gmail (free)

```
python -m tests.integration.test_gmail
```

Sends the PDF order email from a random shipper account to the trucking inbox, waits for it to arrive, and downloads the PDF back. Passes if the PDF matches the original. Confirms sending, reading, and attachments all work.

Don't run this while the inbox watcher (`main.py` in `inbox` mode) is running, or the agent will process the test email.

### 3. Agent (uses Anthropic credits, about $0.10–0.20)

```
python -m tests.integration.test_intake_agent --live
```

Runs Claude on 3 emails, one for each type of decision:

| Email | Expected decision |
|---|---|
| 001 – no PO number | Ask the shipper for the missing PO |
| 002 – offers $3,600 (standard is $4,600) | Check the quote, then send it to Slack |
| 003 – complete order | Create the load ($2,500) |

Prints what Claude chose for each email, the reply it would send, and the cost of every call. Replies are printed, **not** emailed. Loads go to a throwaway Mongo database that's deleted at the end. It does post one real Slack message. Stops at the first wrong result to save credits.

### 4. Full demo run (uses Anthropic credits, about $0.25–0.40)

This is the real end-to-end run.

1. In `src/config/params.py`, set `MODE = "inbox"`.
2. Start the inbox watcher and leave it running:
   ```
   python main.py
   ```
3. In a second terminal, send the 5 order emails:
   ```
   python -m scripts.send_order_requests
   ```

Start the watcher first. It only looks back 30 seconds when it starts. The 5 emails go out over about 20 seconds, and the whole run takes 2–3 minutes.

**What you should see:**

| Email | Terminal shows | Result |
|---|---|---|
| 001 Riverbend Paper – no PO | `request_missing_info` | Reply asking for the PO number |
| 002 Valley Crest Farms – $3,600 offer | `approve_quote` → `raise_quote_for_approval` | Slack approval message + "reviewing your rate" reply |
| 003 Southern Iron – complete | `create_load` | Load saved ($2,500) + confirmation reply |
| 004 Northfield – details in a PDF | `create_load` | Load saved ($2,450) + confirmation reply |
| 005 Lakeshore Beverage – no pickup address | `request_missing_info` | Reply asking for the pickup address |

Where to look:
- **Shipper inboxes:** each reply lands in the same thread as the order. Shipper accounts are shuffled every run, so which account gets which reply changes.
- **Slack:** one "Quote needs approval" message.
- **MongoDB:** database `runbook_trucking`, collection `loads`, with 2 loads.

Press `Ctrl+C` to stop the watcher.

**Before running it again:** delete the `loads` collection (or the `runbook_trucking` database) so you start clean. Each run sends 5 new emails.

---

## Project layout

```
main.py                       Starts the app; MODE in params.py picks what runs
skills/intake.md              The instructions Claude follows for each email
data/                         Test emails, order spec, and standard prices
assets/                       The PDF attached to email 004
scripts/send_order_requests.py  Sends the 5 test emails (plays the shippers)
src/agents/intake/            The agent, its tools, and the reply templates
src/orchestrator/             Inbox watcher (inbox mode) and Gmail sign-in (auth mode)
src/services/                 Gmail, Claude, MongoDB, and Slack connections
src/config/                   Accounts and settings
tests/integration/            The tests above
```
