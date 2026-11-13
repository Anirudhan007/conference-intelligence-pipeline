"""Shared polite HTTP client used by every live-capture adapter.

Rules enforced here, for all adapters:
* robots.txt is fetched first; an unknown policy (network error, 5xx) stops capture (fail closed);
* one request at a time with a minimum delay, ``Retry-After`` honoured on 429, bounded retries;
* an attributable User-Agent; a contact address is included only when the operator supplies one;
* no cookies are persisted, no authentication, no bot-challenge handling.
"""
from __future__ import annotations

import datetime as dt
import time
import urllib.parse
import urllib.robotparser

import requests

USER_AGENT_PREFIX = "ConferenceIntelligencePipeline/0.3"


class CaptureBlocked(RuntimeError):
    """Raised when robots.txt or a site rule forbids the request; never retried or bypassed."""


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def build_user_agent(contact: str | None = None) -> str:
    """Attributable UA. ``contact`` (email or URL) is optional; it is never invented."""
    contact = (contact or "").strip()
    if contact and any(c.isspace() for c in contact):
        raise ValueError("contact must not contain whitespace")
    return f"{USER_AGENT_PREFIX} (+research{'; contact: ' + contact if contact else ''})"


class PoliteHttp:
    def __init__(self, user_agent: str, delay: float = 1.5, timeout: float = 90, max_attempts: int = 5):
        if delay < 1.0:
            raise ValueError("delay must be at least 1.0 second")
        self.user_agent, self.delay, self.timeout, self.max_attempts = user_agent, delay, timeout, max_attempts
        self.session = requests.Session()
        self.session.headers["User-Agent"] = user_agent
        self.errors: list[list] = []
        self.n429 = 0
        self.requests = 0
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self._last = 0.0

    # -------------------------------------------------------------------------------- robots
    def _robots_for(self, url: str) -> urllib.robotparser.RobotFileParser:
        parts = urllib.parse.urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._robots:
            parser = urllib.robotparser.RobotFileParser()
            try:
                r = self.session.get(origin + "/robots.txt", timeout=30)
            except requests.RequestException as exc:
                self._robots[origin] = None
                self.errors.append([origin + "/robots.txt", f"robots fetch failed: {exc}"])
            else:
                if r.status_code == 200:
                    parser.parse(r.text.splitlines())
                    self._robots[origin] = parser
                elif r.status_code in (404, 410):
                    parser.parse([])  # confirmed absent: normal public access
                    self._robots[origin] = parser
                else:
                    self._robots[origin] = None
                    self.errors.append([origin + "/robots.txt", r.status_code])
        parser = self._robots[origin]
        if parser is None:
            raise CaptureBlocked(f"robots.txt policy for {origin} could not be established; capture stopped")
        return parser

    def allowed(self, url: str) -> bool:
        return self._robots_for(url).can_fetch(self.user_agent, url)

    # -------------------------------------------------------------------------------- fetch
    def _pace(self) -> None:
        wait = self.delay - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()

    def get(self, url: str, params: dict | None = None) -> requests.Response | None:
        full = url + ("?" + urllib.parse.urlencode(params) if params else "")
        if not self.allowed(full):
            self.errors.append([full, "robots.txt disallow"])
            raise CaptureBlocked(f"robots.txt disallows {full}")
        for attempt in range(self.max_attempts):
            self._pace()
            self.requests += 1
            try:
                r = self.session.get(full, timeout=self.timeout)
            except requests.RequestException as exc:
                self.errors.append([full, str(exc)])
                time.sleep(5 * (attempt + 1))
                continue
            if r.status_code == 429:
                self.n429 += 1
                self.delay = min(self.delay + 0.5, 6.0)
                try:
                    retry_after = int(r.headers.get("Retry-After", "0") or 0)
                except ValueError:
                    retry_after = 0
                time.sleep(max(retry_after, 15 * (attempt + 1)))
                continue
            if r.status_code in (500, 502, 503, 504) or (r.status_code == 403 and attempt < 2):
                self.errors.append([full, r.status_code])
                time.sleep(5 * (attempt + 1))
                continue
            if r.ok:
                return r
            self.errors.append([full, r.status_code])
            return None
        return None
