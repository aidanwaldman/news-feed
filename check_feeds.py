#!/usr/bin/env python3
"""
wire-desk: poll venture news feeds, push seed/pre-seed raises to ntfy.
Runs on GitHub Actions. State (seen items) is stored in seen.json,
committed back to the repo after each run.

Uses only the Python standard library - nothing to pip install.
"""

import json
import re
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

# ---------------------------------------------------------------- config

NTFY_TOPIC = "aw-wire-desk-576dbd"

FEEDS = [
    ("PR Newswire VC",
     "https://www.prnewswire.com/rss/financial-services-latest-news/venture-capital-list.rss"),
    ("TechCrunch Venture",
     "https://techcrunch.com/category/venture/feed/"),
]

SEEN_FILE = Path(__file__).parent / "seen.json"
SEEN_CAP = 500  # keep the state file small

# ---------------------------------------------------------------- helpers

def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (wire-desk)"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", " ", text or "").strip()


def parse_items(xml_bytes: bytes):
    """Yield (id, title, link, description) for each <item> in an RSS feed."""
    root = ET.fromstring(xml_bytes)
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        guid = (item.findtext("guid") or "").strip()
        desc = strip_html(item.findtext("description") or "")
        uid = guid or link or title
        if uid:
            yield uid, title, link, desc


def notify(source: str, title: str, link: str, desc: str):
    # Source is the banner's bold top line; the headline is the body.
    payload = json.dumps({
        "topic": NTFY_TOPIC,
        "title": source,
        "message": title[:400],
        "click": link,
        "tags": ["moneybag"],
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
    first_run = not SEEN_FILE.exists()
    seen = json.loads(SEEN_FILE.read_text()) if not first_run else []
    seen_set = set(seen)
    new_ids = []
    pushed = 0

    for source, url in FEEDS:
        try:
            xml_bytes = fetch(url)
        except Exception as e:
            print(f"WARN: failed to fetch {source}: {e}")
            continue

        for uid, title, link, desc in parse_items(xml_bytes):
            if uid in seen_set:
                continue
            seen_set.add(uid)
            new_ids.append(uid)

            if first_run:
                continue  # seed the state silently, no notification blast

            try:
                notify(source, title, link, desc)
                pushed += 1
                print(f"PUSHED: {title}")
            except Exception as e:
                print(f"WARN: ntfy push failed for '{title}': {e}")

    # newest ids go on the end; keep only the most recent SEEN_CAP
    seen = (seen + new_ids)[-SEEN_CAP:]
    SEEN_FILE.write_text(json.dumps(seen, indent=0))

    print(f"Done. {len(new_ids)} new items, {pushed} pushed."
          + (" (first run - state seeded silently)" if first_run else ""))


if __name__ == "__main__":
    main()
