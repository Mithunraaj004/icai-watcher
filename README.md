# ICAI Chennai Orientation Course Seat Watcher

A GitHub Actions cloud checker that monitors the public ICAI Launch Batch Details page and sends Android push alerts through ntfy.

## Current monitoring scope

- Region: Southern
- POU: CHENNAI only
- Course: ICITSS - Orientation Course
- Eligible batch From Date: 12 January 2027 onwards (inclusive; no upper date limit)
- All morning, afternoon and evening sessions
- Alerts: newly listed eligible batches and changes in Available Seats
- The alert includes batch number, seat count, dates, displayed time, session classification, Open For, venue, and ICAI links.

The watcher does not register or reserve seats. It does not log into ICAI. A positive seat count is only the count shown by the public listing, not a guarantee that a seat will still be available when you register.

## Android setup

1. Install the ntfy app from Google Play.
2. Subscribe to your private topic on server `https://ntfy.sh`.
3. Keep the topic name private. Anyone who knows a public ntfy.sh topic can publish to or subscribe to it.

## GitHub setup (one time)

1. In this repository, open **Settings → Secrets and variables → Actions**.
2. Select **New repository secret**.
3. Name it exactly `NTFY_TOPIC`.
4. Set the value to the exact topic name you subscribed to in the Android ntfy app.
5. Save the secret. Never put the topic name in source code or share it publicly.

## Test the notification

1. Open the repository's **Actions** tab.
2. Select **ICAI Chennai Seat Watcher**.
3. Choose **Run workflow**.
4. Set **send_test** to `true`.
5. Start the workflow. A test push should arrive in ntfy. The same run will also perform one ICAI check.

## Schedule and limitations

- The workflow is configured for a five-minute cron interval.
- GitHub Actions scheduled events are best-effort and may be delayed, especially during periods of high GitHub load. Five minutes is not a guaranteed check or delivery time.
- The repository is public, so the standard GitHub-hosted runner is intended to run without consuming private-repository Actions minutes. GitHub policies and quotas may change.
- The checker uses the public ICAI page. If ICAI changes its page, requires a CAPTCHA, blocks automated access, or does not load the results, the workflow fails and attempts to send an ntfy failure alert.
- State is stored in GitHub Actions cache to suppress repeat alerts and detect seat-count changes. Cache retention and runner availability are controlled by GitHub; keep an eye on workflow runs.

## Files

- `icai_batch_watcher.py`: checker and ntfy notifications
- `.github/workflows/watch.yml`: five-minute scheduled workflow
- `requirements.txt`: Python dependencies
