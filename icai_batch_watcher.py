"""ICAI ICITSS Orientation Course seat watcher  (GitHub Actions + ntfy phone alerts)

Watches ONLY "ICITSS - Orientation Course" for Southern region at two centres:
    * CHENNAI        (ICAI Bhawan, Nungambakkam)   - batches like ICITSSOC__CHENNAI_144
    * Chengalpattu   (ICAI Salai)                  - batches like ICITSSOC__Chengalpattu_36

It re-checks every CHECK_INTERVAL_SEC (default 60 s) for RUN_MINUTES (default: one check) and
sends a phone notification for EVERY change:
    new batch | seats changed | dates / time / open-for changed | batch removed (full or closed)
Batches that start on or after CUTOFF (12-Jan-2027) are marked TARGET and sent as URGENT.

Environment variables
    NTFY_TOPIC          (required) private topic you subscribed to in the ntfy app
    RUN_MINUTES         keep checking for this many minutes (0 = a single check)
    CHECK_INTERVAL_SEC  seconds between checks (default 60)
    EVENT_NAME          github.event_name ("workflow_dispatch" = run started by you -> status message)
    CHAINED             "true" when the run was started automatically by the previous session (no status message)
    SEND_TEST           "true" = only send a test phone notification
    ICAI_URL, NTFY_SERVER, BROWSER_EXECUTABLE, BROWSER_ARGS   (testing overrides, normally unset)
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests
from playwright.sync_api import Error as PWError
from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

# --------------------------------------------------------------------------- settings
URL = os.environ.get("ICAI_URL", "https://www.icaionlineregistration.org/launchbatchdetail.aspx")
LOGIN_URL = "https://www.icaionlineregistration.org/Admin_Module/Login.aspx"
REGION = "Southern"
POUS = ("CHENNAI", "Chengalpattu")
COURSE = "ICITSS - Orientation Course"
CUTOFF = dt.date(2027, 1, 12)            # batches starting on/after this are TARGET batches
LOW_SEATS = 15                            # flag "filling fast" at or below this

RUN_MINUTES = float(os.environ.get("RUN_MINUTES", "0") or 0)
CHECK_INTERVAL_SEC = float(os.environ.get("CHECK_INTERVAL_SEC", "60") or 60)
HEARTBEAT_HOUR_IST = 9                    # daily "still alive" message, sent once a day after this hour (IST)
FAILURE_ALERT_GAP_SEC = 3 * 3600          # do not spam if ICAI is down
REMOVAL_CONFIRMATIONS = 2                 # a batch must be missing this many checks in a row
WATCHDOG_SEC = 300                        # kill the run if one check hangs this long

STATE_FILE = Path("icai_state.json")
DEBUG_DIR = Path("debug")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
EVENT_NAME = os.environ.get("EVENT_NAME", "")
CHAINED_RUN = os.environ.get("CHAINED", "").lower() == "true"   # started automatically by the previous session
BROWSER_EXECUTABLE = os.environ.get("BROWSER_EXECUTABLE", "") or None
BROWSER_ARGS = os.environ.get("BROWSER_ARGS", "").split()
IST = ZoneInfo("Asia/Kolkata")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
TRACKED_FIELDS = (("seats", "Seats"), ("from_date", "From date"), ("to_date", "To date"),
                  ("batch_time", "Batch time"), ("open_for", "Open for"))


class WatcherError(RuntimeError):
    """A problem reading the ICAI page (reported to the phone, retried next cycle)."""


def now_ist() -> dt.datetime:
    return dt.datetime.now(IST)


def log(msg: str) -> None:
    print(f"[{now_ist():%Y-%m-%d %H:%M:%S} IST] {msg}", flush=True)


# --------------------------------------------------------------------------- watchdog
class Watchdog:
    """Ends the process if a single check hangs (the next scheduled run takes over)."""

    def __init__(self, limit: float) -> None:
        self.limit = limit
        self.last = time.time()
        threading.Thread(target=self._run, daemon=True).start()

    def kick(self) -> None:
        self.last = time.time()

    def _run(self) -> None:
        while True:
            time.sleep(5)
            if time.time() - self.last > self.limit:
                print("WATCHDOG: a check hung; ending this run so the next one can start.", flush=True)
                os._exit(1)


# --------------------------------------------------------------------------- notifications
def ascii_header(text: str) -> str:
    """HTTP headers must be latin-1: an en dash or emoji in a title would crash requests."""
    for bad, good in (("\u2013", "-"), ("\u2014", "-"), ("\u2018", "'"), ("\u2019", "'")):
        text = text.replace(bad, good)
    return text.encode("ascii", "replace").decode("ascii").replace("\n", " ")


def send_ntfy(title: str, message: str, priority: str = "high", tags: str = "bell",
              click: str = LOGIN_URL) -> None:
    if not NTFY_TOPIC:
        raise WatcherError("Secret NTFY_TOPIC is missing (Settings > Secrets and variables > Actions).")
    last: Exception | None = None
    for attempt in (1, 2, 3):
        try:
            r = requests.post(
                f"{NTFY_SERVER}/{NTFY_TOPIC}",
                data=message.encode("utf-8"),
                headers={"Title": ascii_header(title), "Priority": priority,
                         "Tags": ascii_header(tags), "Click": click},
                timeout=20,
            )
            r.raise_for_status()
            return
        except requests.RequestException as exc:
            last = exc
            time.sleep(2 * attempt)
    raise WatcherError(f"ntfy send failed after 3 tries: {last}")


def notify(title: str, message: str, **kw: Any) -> bool:
    """Send, but never let a notification problem stop the watcher."""
    log(f"NOTIFY [{kw.get('priority', 'high')}] {ascii_header(title)}")
    try:
        send_ntfy(title, message, **kw)
        return True
    except Exception as exc:
        log(f"NOTIFY FAILED: {exc}")
        return False


# --------------------------------------------------------------------------- helpers
def parse_date(v: str) -> dt.date | None:
    try:
        return dt.datetime.strptime(v.strip(), "%d/%m/%Y").date()
    except (TypeError, ValueError, AttributeError):
        return None


def is_target(row: dict[str, str]) -> bool:
    d = parse_date(row.get("from_date", ""))
    return bool(d and d >= CUTOFF)


def _to24(h: int, m: int, ap: str) -> float:
    return (h % 12) + (12 if ap.upper() == "PM" else 0) + m / 60


def session_of(batch_time: str) -> str:
    """MORNING / AFTERNOON / EVENING from ICAI text such as '7-0-AM to 1-0-PM'.

    ICAI sometimes shows '7-0-PM to 1-30-PM' (ends before it starts). That is impossible,
    so it is reported as a probable morning batch; the raw text is always shown as well.
    """
    t = " ".join((batch_time or "").split())
    m = re.findall(r"(\d{1,2})\s*[-:]\s*(\d{1,2})\s*[-:]?\s*(AM|PM)", t, re.I)
    if not m:
        return "TIME UNCLEAR"
    (h1, m1, a1) = m[0]
    (h2, m2, a2) = m[1] if len(m) > 1 else m[0]
    start, end = _to24(int(h1), int(m1), a1), _to24(int(h2), int(m2), a2)
    if len(m) > 1 and end <= start:
        if _to24(int(h1), int(m1), "AM") < end:
            return "MORNING (probable - ICAI time text looks mistyped)"
        return "TIME UNCLEAR"
    if start < 12:
        return "MORNING"
    if start < 17:
        return "AFTERNOON"
    return "EVENING"


def short_session(s: str) -> str:
    return s.split(" ")[0] if s else ""


# --------------------------------------------------------------------------- state
def load_state() -> dict[str, Any]:
    fresh: dict[str, Any] = {"v": 3, "initialized": False, "seen": {}, "missing": {},
                             "last_heartbeat": 0, "last_heartbeat_date": "", "last_failure_alert": 0,
                             "failing": False}
    try:
        d = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if isinstance(d, dict) and d.get("v") == 3 and isinstance(d.get("seen"), dict):
            for k, v in fresh.items():
                d.setdefault(k, v)
            if not d["last_heartbeat_date"] and d["last_heartbeat"]:      # upgrade from older state
                d["last_heartbeat_date"] = dt.datetime.fromtimestamp(d["last_heartbeat"], IST).strftime("%Y-%m-%d")
            return d
        log("State file has an unknown format; starting a fresh baseline.")
    except FileNotFoundError:
        pass
    except Exception as exc:
        log(f"State file unreadable ({exc}); starting a fresh baseline.")
    return fresh


def save_state(state: dict[str, Any]) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


# --------------------------------------------------------------------------- scraping
FIND_JS = """(label) => {
  const want = label.trim().toLowerCase();
  const sels = [...document.querySelectorAll('select')];
  for (let i = 0; i < sels.length; i++) {
    const opts = [...sels[i].options];
    const k = opts.findIndex(o => o.text.trim().toLowerCase() === want);
    if (k >= 0) return {index: i, k: k, selected: sels[i].selectedIndex === k};
  }
  return null;
}"""

DROPDOWNS_JS = """() => [...document.querySelectorAll('select')].map(s =>
  [...s.options].slice(0, 12).map(o => o.text.trim()))"""

ROWS_JS = """() => [...document.querySelectorAll('tr')]
  .map(tr => [...tr.children].filter(c => c.tagName === 'TD')
                .map(td => td.innerText.replace(/\\s+/g, ' ').trim()))
  .filter(c => c.length >= 8)"""


def dump_debug(page, tag: str) -> None:
    try:
        DEBUG_DIR.mkdir(exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9_-]", "_", tag)
        (DEBUG_DIR / f"{safe}.html").write_text(page.content(), encoding="utf-8")
        page.screenshot(path=str(DEBUG_DIR / f"{safe}.png"), full_page=True)
    except Exception as exc:
        log(f"(could not save debug files: {exc})")


def settle(page) -> None:
    """Give an ASP.NET postback time to begin, then wait for the page to finish loading."""
    time.sleep(0.6)
    for state in ("load", "networkidle"):
        try:
            page.wait_for_load_state(state, timeout=12000)
        except PWTimeout:
            pass
        except PWError:
            pass


def open_page(page) -> None:
    last: Exception | None = None
    for attempt in (1, 2, 3):
        try:
            resp = page.goto(URL, wait_until="domcontentloaded", timeout=45000)
            status = resp.status if resp else 0
            log(f"Opened ICAI page (attempt {attempt}): HTTP {status}, title={page.title()!r}")
            if status >= 400:
                raise WatcherError(f"ICAI site answered HTTP {status} (it may be blocking GitHub's servers).")
            if page.locator("select").count() < 3:
                body = " ".join(page.locator("body").inner_text().split())[:300]
                raise WatcherError(f"ICAI page loaded without its dropdown filters. Page says: {body!r}")
            return
        except (WatcherError, PWError) as exc:
            last = exc
            log(f"Attempt {attempt} to open the page failed: {exc}")
            time.sleep(3 * attempt)
    raise WatcherError(f"Could not open the ICAI page after 3 tries: {last}")


def find_option(page, label: str, timeout: float = 20) -> dict[str, Any]:
    """Wait until some dropdown offers `label` (dependent dropdowns fill after a postback)."""
    end = time.time() + timeout
    while time.time() < end:
        try:
            info = page.evaluate(FIND_JS, label)
            if info:
                return info
        except PWError:
            pass                                   # page navigating; try again
        time.sleep(0.5)
    try:
        seen = page.evaluate(DROPDOWNS_JS)
    except PWError:
        seen = "unavailable"
    raise WatcherError(f"Option '{label}' not found in any dropdown. Dropdowns now show: {seen}")


def apply_filters(page, pou: str) -> None:
    """Select Region, Pou, Course; re-verify because each postback may reset the others."""
    for _ in range(6):
        all_ok = True
        for label in (REGION, pou, COURSE):
            info = find_option(page, label)
            if info["selected"]:
                continue
            all_ok = False
            page.locator("select").nth(info["index"]).select_option(index=info["k"])
            settle(page)
        if all_ok:
            return
    raise WatcherError(f"Could not keep the filters {REGION} / {pou} / {COURSE} selected.")


def body_text(page) -> str:
    try:
        return page.locator("body").inner_text()
    except PWError:
        return ""


def wait_for_table(page, timeout: float) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        t = body_text(page)
        if "Available Seats" in t and "Batch No" in t:
            return True
        time.sleep(0.5)
    return False


def click_get_list(page) -> None:
    btn = page.locator("input[value='Get List' i], button:has-text('Get List'), a:has-text('Get List')")
    end = time.time() + 10
    while btn.count() == 0 and time.time() < end:
        time.sleep(0.5)
    if btn.count() == 0:
        dump_debug(page, "no_get_list_button")
        raise WatcherError("The 'Get List' button was not found on the ICAI page.")
    try:
        page.evaluate("window.__icai_marker = 1")           # vanishes if the page reloads
    except PWError:
        pass
    btn.first.click()
    for _ in range(16):                                      # wait for reload (or ajax refresh)
        time.sleep(0.5)
        try:
            if page.evaluate("window.__icai_marker !== 1"):
                break
        except PWError:
            break
    settle(page)


def read_rows(page) -> list[list[str]]:
    for _ in range(4):
        try:
            return page.evaluate(ROWS_JS)
        except PWError:
            time.sleep(1)
    raise WatcherError("Could not read the result table (the page kept reloading).")


def pou_matches(cell: str, pou: str) -> bool:
    a, b = cell.strip().casefold(), pou.strip().casefold()
    return bool(a) and (a == b or a in b or b in a)


def fetch_pou(page, pou: str) -> dict[str, Any]:
    """Return {'rows': [...], 'valid': bool} for one centre."""
    open_page(page)
    table_seen = False
    raw: list[list[str]] = []
    for attempt in (1, 2):
        apply_filters(page, pou)
        click_get_list(page)
        table_seen = wait_for_table(page, 25 if attempt == 1 else 15)
        if table_seen:
            break
        log(f"{pou}: result table not visible after attempt {attempt}; retrying.")
    raw = read_rows(page)

    rows: dict[str, dict[str, str]] = {}
    other = 0
    for c in raw:
        if parse_date(c[2]) is None:
            continue                                         # header or filler row
        if "orientation" not in c[6].casefold():
            continue
        if not pou_matches(c[5], pou):
            other += 1
            continue
        rows[c[0]] = {"batch_no": c[0], "seats": c[1], "from_date": c[2], "to_date": c[3],
                      "batch_time": c[4], "pou": pou, "course": c[6], "open_for": c[7],
                      "venue": c[8] if len(c) > 8 else "", "session": session_of(c[4])}
    log(f"{pou}: table_seen={table_seen}, {len(rows)} batch row(s)"
        + (f", ignored {other} row(s) of other centres" if other else ""))
    if not table_seen:
        dump_debug(page, f"no_table_{pou}")
    # A result is trustworthy if we saw the table, or the filters are still correctly selected.
    valid = table_seen
    if not valid:
        try:
            valid = all(find_option(page, lbl, timeout=2)["selected"] for lbl in (REGION, pou, COURSE))
        except WatcherError:
            valid = False
    return {"rows": list(rows.values()), "valid": valid}


def fetch_all() -> dict[str, dict[str, Any]]:
    with sync_playwright() as pw:
        kwargs: dict[str, Any] = {"headless": True}
        if BROWSER_EXECUTABLE:
            kwargs["executable_path"] = BROWSER_EXECUTABLE
        if BROWSER_ARGS:
            kwargs["args"] = BROWSER_ARGS
        browser = pw.chromium.launch(**kwargs)
        try:
            ctx = browser.new_context(user_agent=UA, locale="en-IN", timezone_id="Asia/Kolkata",
                                      viewport={"width": 1366, "height": 900})
            page = ctx.new_page()
            page.set_default_timeout(20000)
            page.set_default_navigation_timeout(45000)
            try:
                return {pou: fetch_pou(page, pou) for pou in POUS}
            except WatcherError:
                dump_debug(page, "failure")
                raise
            except Exception as exc:
                dump_debug(page, "failure")
                raise WatcherError(f"{type(exc).__name__}: {exc}") from exc
        finally:
            try:
                browser.close()
            except Exception:
                pass


# --------------------------------------------------------------------------- messages
def fmt_batch(r: dict[str, str]) -> str:
    return (f"Centre: {r['pou']}\n"
            f"Batch: {r['batch_no']}\n"
            f"Seats available: {r['seats']}\n"
            f"Dates: {r['from_date']} to {r['to_date']}\n"
            f"Time (as shown by ICAI): {r['batch_time']}\n"
            f"Session: {r['session']}\n"
            f"Open for: {r['open_for']}\n"
            f"Venue: {r['venue'] or 'n/a'}\n"
            f"Detected: {now_ist():%d-%b %H:%M} IST\n\n"
            f"Register: {LOGIN_URL}")


def status_text(results: dict[str, dict[str, Any]]) -> str:
    lines = []
    for pou, res in results.items():
        lines.append(f"{pou}: {len(res['rows'])} batch(es)")
        for r in res["rows"]:
            tag = "TARGET" if is_target(r) else "before 12-Jan-2027"
            lines.append(f"  - {r['batch_no']} | {r['from_date']} to {r['to_date']} | "
                         f"{short_session(r['session'])} | seats {r['seats']} | {tag}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- change detection
def process(results: dict[str, dict[str, Any]], state: dict[str, Any]) -> int:
    """Compare with the previous state and send alerts. Returns the number of alerts sent."""
    seen: dict[str, Any] = state["seen"]
    missing: dict[str, int] = state["missing"]
    baseline = not state["initialized"]
    alerts = 0

    for pou, res in results.items():
        prefix = pou.casefold() + "|"
        current = {prefix + r["batch_no"]: r for r in res["rows"]}

        for key, r in current.items():
            missing.pop(key, None)
            prev = seen.get(key)
            target = is_target(r)
            prio = "urgent" if target else "high"
            label = "TARGET " if target else ""
            if prev is None:
                if baseline and not target:
                    pass                                      # first run: just learn existing batches
                else:
                    notify(f"NEW {label}BATCH - {pou} - {short_session(r['session'])}",
                           "NEW BATCH LISTED\n\n" + fmt_batch(r),
                           priority=prio, tags="rotating_light,new" if target else "new")
                    alerts += 1
            else:
                changes = [(name, prev.get(f), r.get(f)) for f, name in TRACKED_FIELDS
                           if str(prev.get(f)) != str(r.get(f))]
                if changes:
                    detail = "\n".join(f"{name}: {old} -> {new}" for name, old, new in changes)
                    seat_change = any(n == "Seats" for n, _, _ in changes)
                    low = False
                    if seat_change:
                        try:
                            low = int(r["seats"]) <= LOW_SEATS
                        except ValueError:
                            pass
                    head = ("SEATS FILLING FAST" if low else "SEATS CHANGED") if seat_change else "DETAILS CHANGED"
                    notify(f"{head} - {pou} {label}".strip() + f" - {r['batch_no'][-12:]}",
                           f"{head}\n{detail}\n\n" + fmt_batch(r),
                           priority="urgent" if (target or low) else "high", tags="bell")
                    alerts += 1
            seen[key] = r

        if res["valid"]:                                      # only trust "missing" on a valid result
            for key in [k for k in seen if k.startswith(prefix) and k not in current]:
                missing[key] = missing.get(key, 0) + 1
                if missing[key] >= REMOVAL_CONFIRMATIONS:
                    old = seen.pop(key)
                    missing.pop(key, None)
                    notify(f"BATCH GONE (full/closed?) - {pou} - {old['batch_no'][-12:]}",
                           "BATCH NO LONGER LISTED - seats may be full or registration closed.\n\n"
                           + fmt_batch(old),
                           priority="urgent" if is_target(old) else "high", tags="x")
                    alerts += 1

    state["initialized"] = True
    return alerts


def daily_status_due(state: dict[str, Any], now: dt.datetime | None = None) -> bool:
    """True once per IST calendar day, after HEARTBEAT_HOUR_IST."""
    now = now or now_ist()
    return now.hour >= HEARTBEAT_HOUR_IST and state.get("last_heartbeat_date") != now.strftime("%Y-%m-%d")


# --------------------------------------------------------------------------- main loop
def main() -> int:
    if not NTFY_TOPIC:
        log("ERROR: secret NTFY_TOPIC is missing or empty.")
        return 2

    if os.environ.get("SEND_TEST", "").lower() == "true":
        ok = notify("ICAI watcher test", "Phone alerts are working. Watching CHENNAI and Chengalpattu "
                    f"(Orientation Course) for batches from {CUTOFF:%d-%b-%Y}.",
                    priority="default", tags="white_check_mark")
        return 0 if ok else 1

    watchdog = Watchdog(WATCHDOG_SEC)
    state = load_state()
    deadline = time.time() + RUN_MINUTES * 60 if RUN_MINUTES > 0 else None
    ok_checks = 0
    first = True
    log(f"Watcher started: every {CHECK_INTERVAL_SEC:.0f}s for "
        f"{'one check' if deadline is None else f'{RUN_MINUTES:.0f} min'}; centres: {', '.join(POUS)}")

    while True:
        started = time.time()
        watchdog.kick()
        try:
            results = fetch_all()
            ok_checks += 1
            if state.get("failing"):
                notify("ICAI watcher recovered", "The ICAI page can be read again. Monitoring continues.",
                       priority="default", tags="white_check_mark")
                state["failing"] = False
            alerts = process(results, state)
            summary = status_text(results)
            if first:
                log("Current listing:\n" + summary)
            manual = EVENT_NAME == "workflow_dispatch" and not CHAINED_RUN and first
            if manual or daily_status_due(state):
                why = "manual check" if manual else "daily status"
                notify(f"ICAI watcher OK - {why}",
                       f"Orientation Course, checking about every {CHECK_INTERVAL_SEC:.0f}s. "
                       f"Target: batches from {CUTOFF:%d-%b-%Y}.\n\n{summary}",
                       priority="default", tags="white_check_mark")
                state["last_heartbeat"] = time.time()
                state["last_heartbeat_date"] = now_ist().strftime("%Y-%m-%d")
            log(f"Check OK. Alerts sent this check: {alerts}")
        except WatcherError as exc:
            log(f"CHECK FAILED: {exc}")
            state["failing"] = True
            gap = time.time() - state["last_failure_alert"]
            if gap > FAILURE_ALERT_GAP_SEC or (EVENT_NAME == "workflow_dispatch" and not CHAINED_RUN and first):
                notify("ICAI watcher: check failed", f"{exc}\n\nCheck manually: {URL}",
                       priority="default", tags="warning", click=URL)
                state["last_failure_alert"] = time.time()
        except Exception as exc:                              # unexpected bug: keep the loop alive
            log(f"UNEXPECTED ERROR: {type(exc).__name__}: {exc}\n{traceback.format_exc()}")
            state["failing"] = True
        save_state(state)
        first = False

        if deadline is None:
            break
        wait = CHECK_INTERVAL_SEC - (time.time() - started)
        if time.time() + max(wait, 0) + 5 > deadline:
            break
        if wait > 0:
            time.sleep(wait)

    log(f"Run finished. Successful checks: {ok_checks}")
    return 0 if ok_checks else 1


if __name__ == "__main__":
    sys.exit(main())
