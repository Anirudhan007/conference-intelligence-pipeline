"""EventScribe (Cadmium) adapter: CTOS, SNO and other meetings hosted on <meeting>.eventscribe.net.

Capture reuses capture/capture.py (public listing pages + ajax detail cards, robots-checked, paced);
parsing reuses pipeline/parse.py inside :func:`pipeline.parse.conference_context`, so CTOS behaviour is
unchanged and other EventScribe meetings get their own ids, timezone and URLs.
"""
from __future__ import annotations

import re
import urllib.parse
from typing import Any

from .base import Adapter
from .http import build_user_agent


def _base(config) -> str:
    parts = urllib.parse.urlsplit(config.source_url)
    host = parts.netloc.lower()
    if not (host.endswith(".eventscribe.net") or host.endswith(".eventscribe.com")):
        from ..config import ConfigurationError

        raise ConfigurationError(f"eventscribe adapter needs an *.eventscribe.net sourceUrl, got {host}")
    return f"https://{host}"


class EventScribeAdapter(Adapter):
    name = "eventscribe-v1"
    platform = "eventscribe"
    access_basis = ("Public, unauthenticated programme pages; robots.txt checked per host before capture "
                    "(EventScribe hosts serve no Disallow rules); paced single-threaded requests.")

    def validate_config(self, config) -> None:
        _base(config)

    def capture(self, config, *, contact=None, delay=1.5, limit=None) -> dict[str, Any]:
        from capture.capture import capture_eventscribe  # repository-local capture module

        bundle = capture_eventscribe(_base(config), contact, limit, delay, user_agent=build_user_agent(contact))
        bundle["source"].update({"conferenceId": config.conference_id, "adapter": self.name})
        return bundle

    def parse(self, bundle, config) -> dict[str, Any]:
        from pipeline import parse

        public = next((r["releaseUtc"] for r in config.embargo_rules if r.get("class") == "public"), None)
        conf = config.raw["conference"]
        with parse.conference_context(config.conference_id, config.timezone, _base(config), public, conf):
            parsed = parse.build(bundle, "D")
        parsed["conference"]["platform"] = self.platform
        return parsed

    def inventory(self, bundle) -> dict[str, list[str]]:
        listings = bundle.get("listings") or {}
        html = lambda name: (listings.get(name) or {}).get("html") or ""  # noqa: E731
        return {
            "posters": sorted(set(re.findall(r'data-posterid="(\d+)"', html("posters")))),
            "presentations": sorted(set(re.findall(r'data-presid="(\d+)"', html("presentations")))),
        }
