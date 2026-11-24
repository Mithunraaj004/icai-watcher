"""ICAI ICITSS Orientation Course seat watcher for GitHub Actions + ntfy.

Monitors BOTH Southern / CHENNAI and Southern / Chengalpattu.
Only batches with From Date >= 12 January 2027 are eligible.
Alerts on new eligible batches and changes to Available Seats.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

import requests
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

URL = "https://www.icaionlineregistration.org/launchbatchdetail.aspx"
REGISTRATION_URL = "https://www.icaionlineregistration.org/Admin_Module/Login.aspx"
REGION = "Southern"
POUS = ("CHENNAI", "Chengalpattu")
COURSE = "ICITSS - Orientation Course"
CUTOFF = dt.date(2027, 1, 12)
STATE_FILE = Path("icai_state.json")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")


class WatcherError(RuntimeError):
    """Raised when the public ICAI listing cannot be checked reliably."""


def log(message: str) -> None:
    stamp = dt.datetime.now(dt.timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    print(f"[{stamp}] {message}", flush=True)


def send_ntfy(title: str, message: str, *, priority: str = "high", tags: str = "bell") -> None:
    if not NTFY_TOPIC:
        raise WatcherError("GitHub Actions secret NTFY_TOPIC is missing. Add it under Settings > Secrets and variables > Actions.")
    # The topic is deliberately read only from an Actions secret; never print it.
    endpoint = f"{NTFY_SERVER}/{NTFY_TOPIC}"
    try:
        response = requests.post(
            endpoint,
            data=message.encode("utf-8"),
            headers={
                "Title": title,
                "Priority": priority,
                "Tags": tags,
                "Content-Type": "text/plain; charset=utf-8",
            },
            timeout=20,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        raise WatcherError(f"ntfy notification failed: {exc}") from exc


def parse_date(value: str) -> dt.date | None:
    try:
        return dt.datetime.strptime(value.strip(), "%d/%m/%Y").date()
    except (TypeError, ValueError):
        return None


def session_of(batch_time: str) -> str:
    """Classify the displayed start time, without guessing/correcting ICAI's text."""
    match = re.search(r"(\d{1,2})\s*[-:]\s*(\d{1,2})\s*[-:]?\s*(AM|PM)", batch_time, re.I)
    if not match:
        return "Time unclear"
    hour, minute, ampm = int(match.group(1)), int(match.group(2)), match.group(3).upper()
    if not (1 <= hour <= 12 and 0 <= minute <= 59):
        return "Time unclear"
    hour24 = (hour % 12) + (12 if ampm == "PM" else 0)
    if hour24 < 12:
        return "Morning"
    if hour24 < 17:
        return "Afternoon"
    return "Evening"


def load_state() -> dict[str, Any]:
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("seen"), dict):
            raise ValueError("invalid state structure")
        return data
    except FileNotFoundError:
        return {"seen": {}}
    except Exception as exc:
        log(f"State file could not be read; starting a fresh baseline: {exc}")
        return {"seen": {}}


def save_state(state: dict[str, Any]) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def wait_for_postback(page) -> None:
    # ICAI uses ASP.NET AutoPostBack for its dependent dropdown filters.
    try:
        page.wait_for_load_state("domcontentloaded", timeout=15000)
    except PlaywrightTimeoutError:
        pass
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except PlaywrightTimeoutError:
        pass


def choose_option(page, select_index: int, label: str) -> None:
    select = page.locator("select").nth(select_index)
    if select.count() == 0:
        raise WatcherError(f"Expected filter dropdown #{select_index + 1} was not found.")
    available = [x.strip() for x in select.locator("option").all_inner_texts()]
    chosen = next((x for x in available if x.casefold() == label.casefold()), None)
    if chosen is None:
        raise WatcherError(f"Filter option '{label}' is not available in dropdown #{select_index + 1}. Options: {available}")
    select.select_option(label=chosen)
    wait_for_postback(page)


def extract_rows(page, selected_pou: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    tables = page.locator("table")
    for table_index in range(tables.count()):
        table = tables.nth(table_index)
        for tr in table.locator("tr").all():
            cells = [" ".join(x.split()) for x in tr.locator("td").all_inner_texts()]
            if len(cells) < 8 or parse_date(cells[2]) is None:
                continue
            pou = cells[5].strip()
            course = cells[6].strip()
            if pou.casefold() != selected_pou.casefold() or course.casefold() != COURSE.casefold():
                continue
            venue = cells[8] if len(cells) >= 9 else ""
            rows.append({
                "batch_no": cells[0],
                "available_seats": cells[1],
                "from_date": cells[2],
                "to_date": cells[3],
                "batch_time": cells[4],
                "pou_name": pou,
                "course": course,
                "open_for": cells[7],
                "course_venue": venue,
                "session": session_of(cells[4]),
            })
    # Avoid duplicated rows from nested layout tables.
    unique: dict[str, dict[str, str]] = {}
    for row in rows:
        unique[row["batch_no"]] = row
    return list(unique.values())


def fetch_pou_rows(page, selected_pou: str) -> list[dict[str, str]]:
    # Re-select the POU separately for each centre. Region and Course remain fixed.
    choose_option(page, 1, selected_pou)
    get_list = page.locator("input[type='submit'][value='Get List'], button:has-text('Get List')")
    if get_list.count() == 0:
        raise WatcherError("The ICAI Get List button was not found after selecting filters.")
    get_list.first.click()
    wait_for_postback(page)
    page.wait_for_timeout(1000)
    body_text = page.locator("body").inner_text()
    if "Batch No" not in body_text or "Available Seats" not in body_text:
        raise WatcherError(f"The ICAI result table did not load for POU {selected_pou}.")
    rows = extract_rows(page, selected_pou)
    log(f"{selected_pou}: read {len(rows)} result row(s).")
    return rows


def fetch_all_pous() -> dict[str, list[dict[str, str]]]:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_default_timeout(20000)
        try:
            page.goto(URL, wait_until="domcontentloaded", timeout=45000)
            if page.locator("select").count() < 3:
                raise WatcherError("The public ICAI page did not expose the expected three filters.")
            choose_option(page, 0, REGION)
            # The Course filter is selected once; the POU is changed separately below.
            choose_option(page, 2, COURSE)
            results: dict[str, list[dict[str, str]]] = {}
            for selected_pou in POUS:
                results[selected_pou] = fetch_pou_rows(page, selected_pou)
            return results
        except WatcherError:
            raise
        except Exception as exc:
            raise WatcherError(f"Unable to read the ICAI listing: {type(exc).__name__}: {exc}") from exc
        finally:
            browser.close()


def format_batch(row: dict[str, str]) -> str:
    venue = row.get("course_venue") or "Not shown in public results"
    seats = row.get("available_seats") or "Not shown"
    return (
        f"Centre: {row['pou_name']}\n"
        f"Batch No: {row['batch_no']}\n"
        f"Available Seats: {seats}\n"
        f"From: {row['from_date']} | To: {row['to_date']}\n"
        f"Batch Time: {row['batch_time']}\n"
        f"Session: {row['session']}\n"
        f"Open For: {row['open_for']}\n"
        f"Course Venue: {venue}\n"
        f"Course: {row['course']}\n\n"
        f"Check/register: {URL}\n"
        f"Registration login: {REGISTRATION_URL}"
    )


def run_test_notification() -> None:
    send_ntfy(
        "ICAI Seat Watcher – Test",
        "Your Android ntfy notification is connected. Monitoring: Southern / CHENNAI + Chengalpattu / ICITSS - Orientation Course. Eligible From Date: 12-Jan-2027 onwards. Both centres and all session times are included.",
        priority="default",
        tags="white_check_mark",
    )
    log("Test notification sent to ntfy.")


def main() -> int:
    if not NTFY_TOPIC:
        log("ERROR: GitHub Actions secret NTFY_TOPIC is missing.")
        return 2

    if os.environ.get("SEND_TEST_NOTIFICATION", "false").lower() == "true":
        try:
            run_test_notification()
        except WatcherError as exc:
            log(f"ERROR: {exc}")
            return 2
        return 0

    try:
        results_by_pou = fetch_all_pous()
    except WatcherError as exc:
        log(f"CHECK FAILED: {exc}")
        try:
            send_ntfy("ICAI watcher – check failed", str(exc), priority="high", tags="warning")
        except WatcherError as notify_exc:
            log(f"Could not send failure notification: {notify_exc}")
        return 1

    state = load_state()
    seen: dict[str, Any] = state.setdefault("seen", {})
    alerts_sent = 0
    eligible_total = 0

    for selected_pou in POUS:
        rows = results_by_pou[selected_pou]
        eligible: dict[str, dict[str, str]] = {}
        for row in rows:
            start_date = parse_date(row["from_date"])
            if start_date and start_date >= CUTOFF:
                eligible[row["batch_no"]] = row

        eligible_total += len(eligible)
        log(f"{selected_pou}: {len(eligible)} eligible batch(es) from {CUTOFF:%d-%m-%Y} onwards.")

        for batch_no, row in eligible.items():
            # Prefix the key with POU so that records from the two centres cannot collide.
            state_key = f"{selected_pou.casefold()}|{batch_no}"
            previous = seen.get(state_key)
            if previous is None:
                send_ntfy(
                    f"NEW ICAI BATCH – {selected_pou}",
                    format_batch(row),
                    priority="urgent",
                    tags="rotating_light,new",
                )
                alerts_sent += 1
                log(f"New eligible batch at {selected_pou}: {batch_no}")
            elif str(previous.get("available_seats", "")) != str(row.get("available_seats", "")):
                old_seats = previous.get("available_seats", "Unknown")
                new_seats = row.get("available_seats", "Unknown")
                send_ntfy(
                    f"SEATS CHANGED – {selected_pou}",
                    f"Batch: {batch_no}\nAvailable seats changed: {old_seats} -> {new_seats}\n\n{format_batch(row)}",
                    priority="urgent",
                    tags="bell,information_source",
                )
                alerts_sent += 1
                log(f"Seat count changed at {selected_pou} for {batch_no}: {old_seats} -> {new_seats}")
            seen[state_key] = row

    save_state(state)
    log(
        f"Check completed successfully for {', '.join(POUS)}. "
        f"Eligible batches: {eligible_total}. Alerts sent: {alerts_sent}."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
