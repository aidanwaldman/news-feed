#!/usr/bin/env python3
"""
formd_check.py: poll EDGAR's live filing feed for new Form Ds, apply
static filters, and surface early-stage tech equity raises within minutes
of filing. Runs every 10 minutes on weekdays via GitHub Actions.

SHADOW MODE: when SHADOW = True, no notifications are sent. Instead, every
filing and its pass/fail verdict is written to formd_log/<date>.md and
committed to the repo, so the filter can be reviewed and tuned before going
live. Flip SHADOW to False to start pushing notifications.

Stdlib only. Deterministic: every rule is a checkbox read, number compare,
or string match. No AI anywhere.
"""

import html
import json
import os
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")

def now_et():
    return datetime.now(EASTERN)


def et_stamp(fmt="%I:%M %p"):
    s = f"{now_et():{fmt}}"
    return s.lstrip("0")
from pathlib import Path

# ---------------------------------------------------------------- config

SHADOW = False  # flip to True to test silently (no notifications)

# GitHub Pages base URL for the generated daily pages
PAGES_BASE = "https://aidanwaldman.github.io/news-feed"

NTFY_TOPIC = "aw-wire-desk-576dbd"

MIN_AMOUNT = 250_000
MAX_AMOUNT = 50_000_000

# Industry groups to keep (Form D's fixed menu). "Other" is kept but
# subjected to the name screen below.
KEEP_INDUSTRIES = {
    "Other Technology", "Computers", "Telecommunications", "Other",
    "Business Services",
}

# Static name screen: entities matching these are almost never operating
# startups. Case-insensitive, word-ish boundaries.
NAME_BLOCKLIST = [
    r"\bfund\b", r"\bl\.?p\.?$", r"\bholdings?\b", r"\bcapital\b",
    r"\bpartners\b", r"\bproperties\b", r"\btrust\b", r"\breit\b",
    r"\bacquisition\b", r"\bventures?\b", r"\ba series of\b", r"\bspv\b",
    r"\bopportunit", r"\breal estate\b", r"\bequity\b", r"\bportfolio\b",
]
NAME_RE = re.compile("|".join(NAME_BLOCKLIST), re.IGNORECASE)

# SEC requires a descriptive User-Agent with contact info
UA = "aidan-waldman-newsfeed awaldman@insightpartners.com"

LOG_DIR = Path(__file__).parent / "formd_log"

# ---------------------------------------------------------------- helpers

def fetch(url: str, attempts: int = 3, backoff: int = 30) -> bytes:
    """GET with retries: SEC/EDGAR occasionally refuses a request under
    load. Wait and retry before letting the run fail."""
    last_err = None
    for i in range(attempts):
        try:
            return _fetch_once(url)
        except Exception as e:
            last_err = e
            if i < attempts - 1:
                time.sleep(backoff)
    raise last_err


def _fetch_once(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


LIVE_FEED = ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent"
             "&type=D&company=&dateb=&owner=include&count=100&output=atom")


def get_new_form_ds():
    """Yield (accession_id, xml_url) for Form Ds in the live feed.
    The feed prefix-matches type=D, so filter titles to exactly 'D - '
    (this also excludes D/A amendments)."""
    xml_str = re.sub(rb'xmlns="[^"]*"', b"", fetch(LIVE_FEED))
    root = ET.fromstring(xml_str)
    for entry in root.iter("entry"):
        title = (entry.findtext("title") or "")
        if not title.startswith("D - "):
            continue
        href = entry.find("link").get("href", "")
        m = re.search(r"/Archives/edgar/data/(\d+)/(\d+)/([\d-]+)-index", href)
        if not m:
            continue
        cik, folder, acc = m.group(1), m.group(2), m.group(3)
        xml_url = (f"https://www.sec.gov/Archives/edgar/data/{cik}/"
                   f"{folder}/primary_doc.xml")
        yield acc, xml_url


def t(root, path):
    """Namespace-free findtext."""
    el = root.find(path)
    return (el.text or "").strip() if el is not None and el.text else ""


def parse_filing(xml_bytes: bytes) -> dict:
    # Strip namespaces for simple querying
    xml_str = re.sub(rb'xmlns="[^"]*"', b"", xml_bytes)
    root = ET.fromstring(xml_str)
    sec = root.find(".//typesOfSecuritiesOffered")
    amt_sold = t(root, ".//offeringSalesAmounts/totalAmountSold")
    amt_total = t(root, ".//offeringSalesAmounts/totalOfferingAmount")
    execs, directors = [], []
    for rp in root.findall(".//relatedPersonsList/relatedPersonInfo"):
        fn = t(rp, "relatedPersonName/firstName")
        ln = t(rp, "relatedPersonName/lastName")
        rels = [r.text or "" for r in rp.findall("relatedPersonRelationshipList/relationship")]
        full = f"{fn} {ln}".strip().title()
        if not full:
            continue
        if "Executive Officer" in rels:
            execs.append(full)
        elif "Director" in rels:
            directors.append(full)
    people = execs[:3] or directors[:3]
    return {
        "people": people,
        "name": t(root, ".//primaryIssuer/entityName"),
        "city": t(root, ".//primaryIssuer/issuerAddress/city").title(),
        "state": t(root, ".//primaryIssuer/issuerAddress/stateOrCountry"),
        "industry": t(root, ".//industryGroup/industryGroupType"),
        "is_equity": t(sec, "isEquityType") == "true" if sec is not None else False,
        "is_pooled": t(sec, "isPooledInvestmentFundType") == "true" if sec is not None else False,
        "is_amendment": t(root, ".//typeOfFiling/newOrAmendment/isAmendment") == "true",
        "amount_sold": amt_sold,
        "amount_total": amt_total,
        "first_sale": t(root, ".//typeOfFiling/dateOfFirstSale/value"),
    }


def money(s: str):
    try:
        return int(float(s))
    except (ValueError, TypeError):
        return None  # "Indefinite" or blank


def evaluate(f: dict):
    """Return (passed: bool, reason: str). Deterministic rules only."""
    if f["is_amendment"]:
        return False, "amendment, not a new filing"
    if f["is_pooled"] or f["industry"] == "Pooled Investment Fund":
        return False, "pooled investment fund"
    if not f["is_equity"]:
        return False, "not an equity offering"
    if f["industry"] not in KEEP_INDUSTRIES:
        return False, f"industry excluded ({f['industry'] or 'unknown'})"
    if NAME_RE.search(f["name"]):
        return False, "name matches fund/holding-entity pattern"
    amt = money(f["amount_total"]) or money(f["amount_sold"])
    if amt is None:
        return False, "no parseable amount (indefinite offering)"
    if amt < MIN_AMOUNT:
        return False, f"below ${MIN_AMOUNT:,} (${amt:,})"
    if amt > MAX_AMOUNT:
        return False, f"above ${MAX_AMOUNT:,} (${amt:,})"
    return True, f"${amt:,}"


def fmt_amount(f: dict) -> str:
    amt = money(f["amount_total"]) or money(f["amount_sold"]) or 0
    if amt >= 1_000_000:
        return f"${amt/1_000_000:.1f}M".replace(".0M", "M")
    return f"${amt/1_000:.0f}K"


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


PAGE_CSS = """
body{font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;max-width:760px;
margin:0 auto;padding:0 1rem 3rem;background:#0a1834;color:#eef2f8}
.masthead{border-bottom:2px solid #1f6feb;margin-bottom:1.5rem;padding:2rem 0 1rem}
.prev{margin:2rem 0 1rem;padding-top:1rem;border-top:1px solid #1f2f52;
font-size:.8rem;color:#8aa4c8}
.prev a{color:#4d9bff;text-decoration:none;margin:0 2px}
.prev a:hover{color:#ff8a3d}
.brand{font-size:.75rem;letter-spacing:.18em;text-transform:uppercase;
color:#4d9bff;font-weight:700}
.brand:after{content:'';display:inline-block;width:6px;height:6px;
background:#ff8a3d;border-radius:50%;margin-left:8px;vertical-align:middle}
h1{font-size:1.35rem;font-weight:600;margin:.3rem 0 0}
.sub{color:#8fa3c4;font-size:.9rem;margin-top:.5rem}
.card{background:#0f2145;border:1px solid #1c3563;border-left:3px solid #1f6feb;
border-radius:6px;padding:1.1rem 1.3rem;margin-bottom:1rem}
.amt{font-size:1.15rem;font-weight:700;color:#ff8a3d}
.name{font-size:1.15rem;font-weight:600;margin-left:.4rem;color:#ffffff}
.meta{color:#8fa3c4;margin:.45rem 0 .8rem;font-size:.95rem;line-height:1.5}
.btns a{display:inline-block;background:#16294f;color:#cfe0ff;text-decoration:none;
padding:.4rem .9rem;border-radius:4px;margin-right:.5rem;font-size:.85rem;
border:1px solid #24406f}
.btns a:hover{background:#1f6feb;color:#fff;border-color:#1f6feb}
"""


def fmt_people(people):
    """Render founder/exec names cleanly: 'Jane Doe' or 'Jane Doe and John Roe'
    or 'Jane Doe, John Roe and Sam Poe'. Empty -> ''."""
    names = [html.escape(p) for p in people if p]
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + " and " + names[-1]


def prev_days_footer(d):
    """Links to earlier days' pages so each day stays its own page."""
    docs = Path("docs/formd")
    days = sorted(
        (p.stem for p in docs.glob("????-??-??.html") if p.stem != f"{d:%Y-%m-%d}"),
        reverse=True)[:10]
    if not days:
        return ""
    links = " &middot; ".join(
        f'<a href="{day}.html">{datetime.strptime(day, "%Y-%m-%d"):%a %b %d}</a>'
        for day in days)
    return f'<div class="prev">Previous days: {links}</div>'


def build_page(d, entries):
    """Write docs/formd/<date>.html + index.html. Returns nothing."""
    cards = []
    for f, filing_url, _ in entries:
        s = slug(f["name"])
        google = ("https://www.google.com/search?q="
                  + urllib.parse.quote(f'"{f["name"]}" {f["city"]}'))
        linkedin = ("https://www.linkedin.com/search/results/all/?keywords="
                    + urllib.parse.quote(f["name"]))
        sold, total = money(f["amount_sold"]), money(f["amount_total"])
        amt_detail = (f"raised {fmt_money(sold)} of {fmt_money(total)} offering"
                      if sold and total and sold < total else "")
        meta = " &middot; ".join(x for x in [
            html.escape(f"{f['city']}, {f['state']}"),
            fmt_people(f.get("people", [])),
            amt_detail,
            f"first sale {f['first_sale']}" if f["first_sale"] else "",
            f"published {f['caught']}" if f.get("caught") else "",
        ] if x)
        cards.append(f"""
<div class="card" id="{s}">
  <span class="amt">{fmt_amount(f)}</span><span class="name">{html.escape(f['name'])}</span>
  <div class="meta">{meta}</div>
  <div class="btns">
    <a href="{google}">Google</a>
    <a href="{linkedin}">LinkedIn</a>
    <a href="{filing_url}">SEC Filing</a>
  </div>
</div>""")
    page = f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex">
<title>Form D News {d:%Y-%m-%d}</title><style>{PAGE_CSS}</style></head><body>
<div class="masthead">
<div class="brand">Insight Partners</div>
<h1>Form D News &mdash; {d:%A, %B %d, %Y}</h1>
<div class="sub">{len(entries)} new equity filing{"s" if len(entries) != 1 else ""} today<br>
Updated {now_et():%B %d}, {et_stamp()} ET | Source: SEC EDGAR</div>
</div>
{''.join(cards)}
{prev_days_footer(d)}
</body></html>"""
    docs = Path(__file__).parent / "docs" / "formd"
    docs.mkdir(parents=True, exist_ok=True)
    (docs / f"{d:%Y-%m-%d}.html").write_text(page)
    (docs / "index.html").write_text(page)


def fmt_money(n):
    if n is None:
        return None
    if n >= 1_000_000:
        return f"${n/1_000_000:.1f}M".replace(".0M", "M")
    return f"${n/1_000:.0f}K"


def notify(f: dict, filing_url: str):
    sold, total = money(f["amount_sold"]), money(f["amount_total"])
    if sold and total and sold < total:
        amount_line = f"raised {fmt_money(sold)} of {fmt_money(total)} offering"
    else:
        amount_line = None
    parts = [f"{f['city']}, {f['state']}"]
    if f.get("people"):
        parts.append(", ".join(f["people"]))
    if amount_line:
        parts.append(amount_line)
    if f["first_sale"]:
        parts.append(f"first sale {f['first_sale']}")
    payload = json.dumps({
        "topic": NTFY_TOPIC,
        "title": f"{fmt_amount(f)} - {f['name']}",
        "message": " | ".join(parts),
        "click": f"{PAGES_BASE}/formd/index.html#{slug(f['name'])}",
        "actions": [{"action": "view", "label": "Filing", "url": filing_url}],
        "tags": ["classical_building"],
        "priority": 4,
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://ntfy.sh/",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    urllib.request.urlopen(req, timeout=30)


# ---------------------------------------------------------------- main

def main():
    today = now_et().date()  # Eastern calendar day, not UTC
    seen_file = Path(__file__).parent / "formd_seen.json"
    first_run = not seen_file.exists()
    if first_run:
        seen = {}
    else:
        raw = json.loads(seen_file.read_text())
        # migrate from the old list format to {accession: date_first_seen}
        if isinstance(raw, list):
            seen = {acc: f"{date.today()}" for acc in raw}
        else:
            seen = raw

    data_dir = Path(__file__).parent / "formd_data"
    data_dir.mkdir(exist_ok=True)
    day_file = data_dir / f"{today:%Y-%m-%d}.json"
    day_entries = json.loads(day_file.read_text()) if day_file.exists() else []

    passed_now, rejected_now, errors = [], [], 0

    for acc, xml_url in get_new_form_ds():
        if acc in seen:
            continue
        seen[acc] = f"{today}"
        if first_run:
            continue  # seed silently, no backfill blast
        try:
            f = parse_filing(fetch(xml_url))
        except Exception:
            errors += 1
            continue
        ok, reason = evaluate(f)
        human_url = xml_url.replace("primary_doc.xml", "")
        if ok:
            f["caught"] = f"{et_stamp()} ET"
            passed_now.append((f, human_url))
            day_entries.append([f, human_url])
        else:
            rejected_now.append((f["name"], reason))
        time.sleep(0.15)

    # persist state
    cutoff = f"{today - timedelta(days=30)}"
    seen = {acc: d for acc, d in seen.items() if d >= cutoff}
    seen_file.write_text(json.dumps(seen))
    day_file.write_text(json.dumps(day_entries, default=str))

    # regenerate today's page from the full day so far
    build_page(today, [(f, u, "") for f, u in reversed(day_entries)])

    # append to the day's audit log
    LOG_DIR.mkdir(exist_ok=True)
    log = LOG_DIR / f"{today:%Y-%m-%d}.md"
    if passed_now or rejected_now:
        stamp = f"{et_stamp()} ET"
        lines = [log.read_text()] if log.exists() else [
            f"# Form D filter log for {today:%A %b %d, %Y}\n"]
        lines.append(f"\n### Run at {stamp}\n")
        for f, url in passed_now:
            lines.append(f"- PASS **{fmt_amount(f)} - {f['name']}** | "
                         f"{f['city']}, {f['state']} | {f['industry']} | [filing]({url})")
        for name, reason in rejected_now:
            lines.append(f"- reject {name}: {reason}")
        log.write_text("\n".join(lines))

    # queue notifications for the post-deploy phase
    pending = Path(__file__).parent / "pending.json"
    pending.write_text(json.dumps([[f, u] for f, u in passed_now], default=str))

    print(f"{today}: {len(passed_now)} new passed, {len(rejected_now)} rejected,"
          f" {errors} errors. Shadow={SHADOW}."
          + (" (first run - seeded silently)" if first_run else ""))


def notify_phase():
    """Run after the Pages deploy: send queued notifications."""
    pending = Path(__file__).parent / "pending.json"
    if not pending.exists():
        print("nothing pending")
        return
    entries = json.loads(pending.read_text())
    if SHADOW:
        print(f"SHADOW on: {len(entries)} queued, none sent")
        return
    for f, url in entries:
        try:
            notify(f, url)
            print(f"PUSHED: {f['name']}")
        except Exception as e:
            print(f"WARN: push failed for {f['name']}: {e}")
    pending.write_text("[]")


if __name__ == "__main__":
    if os.environ.get("FORMD_PHASE") == "notify":
        notify_phase()
    else:
        main()
