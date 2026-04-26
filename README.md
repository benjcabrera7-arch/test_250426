# Japan Flight Monitor

This repository runs a scheduled GitHub Actions workflow that checks Philippines-to-Japan round-trip fares and sends alerts when matching fares appear inside the configured threshold.

## Note

API keys, bot tokens, app passwords, chat IDs, and notification addresses are stored in GitHub Actions Secrets, not in code.

## Notifications

The notifier only sends alerts when the matching fare set changes. If the same deal is still available on the next run, it is not sent again.