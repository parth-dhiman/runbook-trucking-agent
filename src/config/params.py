MODE = "inbox"

# Inbox polling: check every POLL_INTERVAL_SECONDS. Each check covers the time since
# the previous one plus LOOKBACK_SECONDS of overlap, so mail Gmail indexes a few
# seconds late isn't missed; messages already seen are skipped.
POLL_INTERVAL_SECONDS = 10
LOOKBACK_SECONDS = 30
