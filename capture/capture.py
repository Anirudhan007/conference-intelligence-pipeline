"""Capture stage: conference URL in, raw bundle out.

Usage:
    python capture/capture.py https://ctos2026.eventscribe.net/index.asp?launcher=1 -o data/raw/bundle.json

Runs anywhere with direct network access to the conference site. Produces exactly the same bundle
format as the browser harvester (capture/eventscribe_harvester.js), so later stages do not care which
capture path was used.

Politeness: one request at a time, 1.5 s gap, backoff on HTTP 429 (Retry-After honoured),
identifiable user agent, public pages only, no cookies, no login.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.robotparser
from pathlib import Path

import requests
from bs4 import BeautifulSoup

USER_AGENT_PREFIX = "ConferenceIntelligencePipeline/0.2"
DETAIL_RE = re.compile(
    r"ajaxcalls/(PosterInfo|PresentationInfo|SessionInfo|ExhibitorInfo)\.asp\?.*?(PosterID|PresentationID|BoothID)=(\d+)", re.I
)
EVENTSCRIBE_LISTINGS = {
    "home": "/index.asp?launcher=1",
    "posters": "/PosterTitles.asp?PosterSortOrder=num&pfp=BrowsebyPosterNumber",
    "postersByCategory": "/PosterTitles.asp?PosterSortOrder=num&pfp=BrowsebyPosterTitle",
    "presentations": "/SearchByPresentation.asp?pfp=BrowsebyTitle",
    "agenda": "/agenda.asp?pfp=BrowsebyDay",
    "sponsorEvents": "/agenda.asp?pfp=BrowsebySP",
    "networking": "/agenda.asp?pfp=BrowsebyNE",
    "ancillary": "/agenda.asp?pfp=BrowsebyAE",
    "spagn": "/agenda.asp?pfp=BrowsebyPA",
    "sponsors": "/sponsors.asp?pfp=Sponsors",
}


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def build_user_agent(contact_email: str) -> str:
    """Build an attributable User-Agent; never send the shipped placeholder to a website."""
    contact_email = (contact_email or "").strip()
    if "@" not in contact_email or any(c.isspace() for c in contact_email):
        raise ValueError("a valid --contact-email (or CONTACT_EMAIL environment variable) is required for live capture")
    return f"{USER_AGENT_PREFIX} (+research; contact: {contact_email})"


def fingerprint(url: str) -> dict:
    """Identify the conference platform from the URL. Unknown platforms stop with a clear message."""
    host = urllib.parse.urlparse(url).netloc.lower()
    if host.endswith("eventscribe.net") or host.endswith("eventscribe.com"):
        return {"platform": "eventscribe", "base": f"https://{host}"}
    raise SystemExit(
        f"Unknown platform for {host}. Add an adapter (see README: Adding a platform) "
        "or run discovery in a browser to propose one."
    )


class Fetcher:
    def __init__(self, base: str, user_agent: str, delay: float = 1.5):
        self.base, self.delay, self.n429 = base, delay, 0
        self.s = requests.Session()
        self.s.headers["User-Agent"] = user_agent
        self.errors: list = []
        self.robots = urllib.robotparser.RobotFileParser()
        self.robots_known = False
        try:
            r = self.s.get(base + "/robots.txt", timeout=20)
            if r.status_code == 200:
                self.robots.parse(r.text.splitlines())
                self.robots_known = True
            elif r.status_code == 404:
                # A confirmed absent robots.txt permits normal public-page access.
                self.robots.parse([])
                self.robots_known = True
            else:
                self.errors.append([base + "/robots.txt", r.status_code])
        except requests.RequestException as exc:
            # Network failure must not be treated as an allow-all robots policy.
            self.errors.append([base + "/robots.txt", f"robots fetch failed: {exc}"])

    def get(self, path: str) -> str | None:
        url = path if path.startswith("http") else self.base + path
        if not self.robots_known:
            self.errors.append([url, "robots.txt policy unknown; capture stopped"])
            return None
        if not self.robots.can_fetch(self.s.headers["User-Agent"], url):
            self.errors.append([url, "robots.txt disallow"])
            return None
        for attempt in range(5):
            try:
                r = self.s.get(url, timeout=30)
            except requests.RequestException as e:
                self.errors.append([url, str(e)])
                time.sleep(3)
                continue
            if r.status_code == 429:
                self.n429 += 1
                self.delay = min(self.delay + 0.5, 5)
                wait = int(r.headers.get("Retry-After", "0") or 0)
                time.sleep(max(wait, 20 * (attempt + 1)))
                continue
            time.sleep(self.delay)
            if r.status_code == 403 and attempt < 4:
                # eventscribe returns transient 403s under load; the same URL succeeds on retry
                self.errors.append([url, 403])
                time.sleep(4 * (attempt + 1))
                continue
            if r.ok:
                return r.text
            self.errors.append([url, r.status_code])
            return None
        return None


def strip(soup_el):
    for sel in ["script", "style", "nav", "#mobilenav", ".navbar-wrapper", "footer", "#google_translate_element", ".popup_header"]:
        for e in soup_el.select(sel):
            e.decompose()
    return soup_el


def norm_detail(du: str):
    m = DETAIL_RE.search(du or "")
    if not m:
        return None
    efp = re.search(r"efp=([A-Za-z0-9]+)", du)
    return f"{m.group(1)}:{m.group(3)}", f"/ajaxcalls/{m.group(1)}.asp?" + (f"efp={efp.group(1)}&" if efp else "") + f"{m.group(2)}={m.group(3)}"


def capture_eventscribe(base: str, contact_email: str, limit: int | None = None, delay: float = 1.5) -> dict:
    """Capture public EventScribe pages while respecting a known robots policy."""
    user_agent = build_user_agent(contact_email)
    f = Fetcher(base, user_agent, delay)
    if not f.robots_known:
        detail = f.errors[-1][1] if f.errors else "unknown error"
        raise RuntimeError(f"cannot establish robots.txt policy for {base}: {detail}")
    bundle = {"source": {"baseUrl": base, "platform": "eventscribe", "userAgent": user_agent, "started": now()},
              "listings": {}, "details": {}, "errors": f.errors}
    listings = dict(EVENTSCRIBE_LISTINGS)
    agenda = f.get(listings["agenda"])
    if agenda:
        for a in BeautifulSoup(agenda, "lxml").select('a[href*="agenda.asp?pfp=days"]'):
            m = re.search(r"day=(\d+)/(\d+)/(\d+)", a["href"])
            if m:
                listings[f"day_{m.group(3)}_{m.group(1)}_{m.group(2)}"] = "/" + a["href"].lstrip("/")
    queue: dict[str, str] = {}
    for name, path in listings.items():
        html = agenda if name == "agenda" else f.get(path)
        if not html:
            continue
        soup = BeautifulSoup(html, "lxml")
        for el in soup.select("[data-url]"):
            r = norm_detail(el.get("data-url"))
            if r and r[0] not in queue:
                queue[r[0]] = r[1]
        main = strip(soup.select_one(".content-wrap") or soup.body)
        bundle["listings"][name] = {"url": base + path, "retrievedAt": now(), "bytes": len(html),
                                    "html": re.sub(r"\s+", " ", main.decode_contents())}
    items = list(queue.items())[: limit or None]
    i = 0
    while i < len(items):
        key, path = items[i]
        i += 1
        html = f.get(path)
        if not html:
            continue
        soup = BeautifulSoup(html, "lxml")
        card = strip(soup.select_one(".card") or soup.select_one(".popup_content") or soup.body)
        bundle["details"][key] = {"url": base + path, "retrievedAt": now(), "bytes": len(html),
                                  "html": re.sub(r"\s+", " ", str(card))}
        if key.startswith(("SessionInfo", "PresentationInfo")):
            for el in card.select("[data-url]"):
                r = norm_detail(el.get("data-url"))
                if r and r[0] not in queue:
                    queue[r[0]] = r[1]
                    items.append(r)
        if i % 50 == 0:
            print(f"{i}/{len(items)} details, 429s={f.n429}", file=sys.stderr)
    bundle["source"]["finished"] = now()
    bundle["rate"] = {"delay": f.delay, "n429": f.n429}
    return bundle


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("url")
    ap.add_argument("-o", "--out", default="data/raw/bundle.json")
    ap.add_argument("--limit", type=int, default=None, help="cap detail pages (for smoke tests)")
    ap.add_argument("--delay", type=float, default=1.5, help="minimum seconds between requests (default: 1.5)")
    ap.add_argument("--contact-email", default=os.getenv("CONTACT_EMAIL"),
                    help="required contact address for the User-Agent; defaults to CONTACT_EMAIL")
    a = ap.parse_args()
    if a.delay < 1.0:
        ap.error("--delay must be at least 1.0 second")
    fp = fingerprint(a.url)
    try:
        bundle = capture_eventscribe(fp["base"], a.contact_email, a.limit, a.delay)
    except ValueError as exc:
        ap.error(str(exc))
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as fh:
        json.dump(bundle, fh)
    print(f"wrote {a.out}: {len(bundle['listings'])} listings, {len(bundle['details'])} details, {len(bundle['errors'])} errors")


if __name__ == "__main__":
    main()
