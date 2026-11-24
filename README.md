# ICAI Chennai + Chengalpattu Orientation Course Seat Watcher

A GitHub Actions cloud checker that monitors two Southern Region POUs on the public ICAI Launch Batch Details page and sends Android push notifications through ntfy.

## Monitoring scope

The checker runs the filters separately for each POU:

1. **CHENNAI** — includes the Nungambakkam course venue when that address is shown by ICAI.
2. **Chengalpattu** — the alternative POU shown in the portal.

For both centres:

- Region: Southern
- Course: ICITSS - Orientation Course
- Eligible batch From Date: 12 January 2027 onwards (inclusive; no upper date limit)
- All morning, afternoon and evening sessions
- Alerts: newly listed eligible batches and changes in Available Seats
- The alert includes POU, batch number, seat count, dates, displayed time, session classification, Open For, venue, and ICAI links.

The two centres are tracked independently, so a batch-number collision between POUs does not mix their seat history.

The watcher does not register or reserve seats. It does not log into ICAI. A positive seat count is only the count shown by the public listing, not a guarantee that a seat will still be available when you register.

## Android ntfy setup

1. Install the ntfy app on Android.
2. Subscribe to your private topic on server `https://ntfy.sh`.
3. Keep the topic name private. Anyone who knows a public ntfy.sh topic can publish to or subscribe to it.

## GitHub secret (required)

1. Open **Settings → Secrets and variables → Actions** in this repository.
2. Select **New repository secret**.
3. Name it exactly `NTFY_TOPIC`.
4. Set the value to the exact topic name you subscribed to in the Android ntfy app.
5. Save. Never put the topic name in source code or share it publicly.

## Test push notification

1. Open the repository's **Actions** tab.
2. Select **ICAI Chennai + Chengalpattu Seat Watcher**.
3. Choose **Run workflow**.
4. Set **send_test** to `true`.
5. Start the workflow. This sends a test notification only; the separate scheduled runs perform ICAI checks.

## Schedule and limitations

- The workflow is configured for a five-minute cron interval.
- GitHub scheduled events are best-effort and may be delayed, especially during periods of high GitHub load. Five minutes is not a guaranteed check or delivery time.
- The repository is public, so the standard GitHub-hosted runner is intended to run without consuming private-repository Actions minutes. GitHub policies and quotas may change.
- The checker uses the public ICAI page. If ICAI changes its page, requires a CAPTCHA, blocks automated access, or does not load the results, the workflow fails and attempts to send an ntfy failure alert.
- State is stored in GitHub Actions cache to suppress repeat alerts and detect seat-count changes. Cache retention and runner availability are controlled by GitHub; keep an eye on workflow runs.

## Files

- `icai_batch_watcher.py`: two-centre checker and ntfy notifications
- `.github/workflows/watch.yml`: five-minute scheduled workflow
- `requirements.txt`: Python dependencies
