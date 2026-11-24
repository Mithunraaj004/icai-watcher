"""
ICAI ICITSS Orientation Course watcher
Monitors CHENNAI (Nungambakkam, ICAI Bhawan) and Chengalpattu (ICAI Salai) together.
Alerts when a batch starting ON/AFTER 12-Jan-2027 appears, its seats change,
or it disappears (full/closed). Every alert says MORNING or AFTERNOON/EVENING.

SETUP (once):
  pip install playwright requests
  playwright install chromium

ALERT CHANNELS (set any you like as environment variables):
  WhatsApp (free, CallMeBot): WA_PHONE=919876543210   WA_APIKEY=yourkey
  Telegram:                   TG_TOKEN=...            TG_CHAT_ID=...
  ntfy.sh push app:           NTFY_TOPIC=some-secret-topic-name

RUN:
  python icai_batch_watcher.py --debug   # visible browser, single run (test)
  python icai_batch_watcher.py           # headless, loops forever
"""
import argparse, datetime as dt, json, os, re, sys, time
import requests
from playwright.sync_api import sync_playwright

URL = "https://www.icaionlineregistration.org/launchbatchdetail.aspx"
REGION = "Southern"
POUS = ["CHENNAI", "Chengalpattu"]           # text as shown in the Pou dropdown
COURSE = "ICITSS - Orientation Course"
CUTOFF = dt.date(2027, 1, 12)                # only batches starting on/after this
LOW_SEATS = 15                               # "filling fast" warning threshold
INTERVAL_SEC = 60                            # one full check (both centres) per minute
HEARTBEAT_HOURS = 24
STATE_FILE = "icai_state.json"

TG_TOKEN = os.environ.get("TG_TOKEN", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")
WA_PHONE = os.environ.get("WA_PHONE", "")
WA_APIKEY = os.environ.get("WA_APIKEY", "")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")


def notify(msg):
    print(f"[{dt.datetime.now():%d-%b %H:%M:%S}] {msg}\n")
    full = msg + "\n\nRegister: " + URL
    if WA_PHONE and WA_APIKEY:
        try:
            requests.get("https://api.callmebot.com/whatsapp.php",
                         params={"phone": WA_PHONE, "text": full, "apikey": WA_APIKEY},
                         timeout=20)
        except Exception as e:
            print("WhatsApp error:", e)
    if NTFY_TOPIC:
        try:
            requests.post(f"https://ntfy.sh/{NTFY_TOPIC}", data=full.encode("utf-8"),
                          headers={"Priority": "urgent", "Title": "ICAI batch alert"},
                          timeout=15)
        except Exception as e:
            print("ntfy error:", e)
    if TG_TOKEN and TG_CHAT_ID:
        try:
            requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                          data={"chat_id": TG_CHAT_ID, "text": full}, timeout=15)
        except Exception as e:
            print("Telegram error:", e)


def parse_date(s):
    try:
        return dt.datetime.strptime(s.strip(), "%d/%m/%Y").date()
    except Exception:
        return None


def session_of(batch_time):
    """MORNING or AFTERNOON/EVENING from text like '7-0-AM to 1-0-PM',
    '1-30-PM to 7-30-PM'. The site sometimes shows '7-0-PM to 1-30-PM'
    (start after end) which can only mean 7 AM - 1:30 PM."""
    t = " ".join(batch_time.split())
    m = re.findall(r"(\d{1,2})\s*[-:]\s*(\d{1,2})\s*[-:]?\s*(AM|PM)", t, re.I)
    if len(m) < 2:
        return "UNKNOWN"

    def to24(h, mi, ap):
        return (int(h) % 12) + (12 if ap.upper() == "PM" else 0) + int(mi) / 60

    start, end = to24(*m[0]), to24(*m[1])
    if end <= start:                      # impossible -> start was really AM
        start = to24(m[0][0], m[0][1], "AM")
    return "MORNING" if start < 12 else "AFTERNOON/EVENING"


def fetch_rows(page, pou):
    page.goto(URL, wait_until="networkidle")

    def choose(idx, text):
        sel = page.locator("select").nth(idx)
        opts = [o.strip() for o in sel.locator("option").all_inner_texts()]
        match = next((o for o in opts if o.lower() == text.lower()), None)
        if match is None:
            raise RuntimeError(f"'{text}' not in dropdown #{idx}: {opts}")
        sel.select_option(label=match)
        page.wait_for_load_state("networkidle")   # ASP.NET postback

    choose(0, REGION)
    choose(1, pou)
    choose(2, COURSE)
    page.locator("input[value='Get List'], button:has-text('Get List')").first.click()
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(1500)

    rows = []
    for tr in page.locator("table tr").all():
        c = [x.strip() for x in tr.locator("td").all_inner_texts()]
        # Batch No | Seats | From | To | Time | Pou | Course | Open For | Venue | Register
        if len(c) >= 8 and parse_date(c[2]) and c[5].strip().lower() == pou.lower():
            rows.append({
                "batch": " ".join(c[0].split()), "seats": c[1], "from": c[2],
                "to": c[3], "time": " ".join(c[4].split()), "pou": c[5],
                "open_for": c[7],
                "venue": " ".join(c[8].split()) if len(c) > 8 else "",
                "session": session_of(c[4]),
            })
    return rows


def fmt(r):
    return (f"{r['pou'].upper()} - {r['batch']}\n"
            f"  {r['session']} batch ({r['time']})\n"
            f"  {r['from']} to {r['to']}\n"
            f"  Seats available: {r['seats']}  |  Open for: {r['open_for']}\n"
            f"  Venue: {r['venue'][:70]}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--debug", action="store_true")
    ap.add_argument("--minutes", type=float, default=0,
                    help="stop after this many minutes (used by GitHub Actions)")
    args = ap.parse_args()
    deadline = time.time() + args.minutes * 60 if args.minutes else None

    try:
        state = json.load(open(STATE_FILE))
    except Exception:
        state = {"seen": {}, "last_heartbeat": 0}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.debug)
        page = browser.new_page()
        fails = 0
        while True:
            try:
                total = 0
                for pou in POUS:
                    rows = fetch_rows(page, pou)
                    total += len(rows)
                    print(f"[{dt.datetime.now():%H:%M:%S}] {pou}: {len(rows)} batches")
                    for r in rows:
                        print("  ", r["batch"], r["from"], r["session"], "seats:", r["seats"])

                    current = set()
                    for r in rows:
                        start = parse_date(r["from"])
                        key = r["batch"]
                        current.add(key)
                        prev = state["seen"].get(key)
                        target = start >= CUTOFF

                        if prev is None:
                            if target:
                                notify(f"NEW TARGET BATCH OPEN!\n{fmt(r)}")
                            else:
                                notify(f"New batch listed (before your cutoff):\n{fmt(r)}")
                        elif target and prev["seats"] != r["seats"]:
                            try:
                                low = int(r["seats"]) <= LOW_SEATS
                            except ValueError:
                                low = False
                            head = "SEATS FILLING FAST" if low else "Seats changed"
                            notify(f"{head}: {prev['seats']} -> {r['seats']}\n{fmt(r)}")
                        state["seen"][key] = r

                    # target batches of this centre that vanished (full / closed)
                    for key, old in list(state["seen"].items()):
                        if old["pou"].lower() == pou.lower() and key not in current:
                            if parse_date(old["from"]) >= CUTOFF:
                                notify(f"Target batch REMOVED (full/closed?):\n{fmt(old)}")
                            del state["seen"][key]

                fails = 0
                if time.time() - state["last_heartbeat"] > HEARTBEAT_HOURS * 3600:
                    notify(f"Watcher alive. Watching {', '.join(POUS)} for batches "
                           f"from {CUTOFF:%d-%b-%Y}. {total} batch(es) listed now.")
                    state["last_heartbeat"] = time.time()
                json.dump(state, open(STATE_FILE, "w"))
            except Exception as e:
                fails += 1
                print("Check failed:", e)
                if fails == 10:
                    notify(f"Watcher has failed 10 times in a row: {e}")
                if args.debug:
                    input("Press Enter to close...")
                    sys.exit(1)

            if args.debug:
                input("Debug run finished. Press Enter to close...")
                break
            if deadline and time.time() + INTERVAL_SEC > deadline:
                break
            time.sleep(INTERVAL_SEC)
        browser.close()


if __name__ == "__main__":
    main()
