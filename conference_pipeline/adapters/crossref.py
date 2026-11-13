"""Crossref journal-supplement adapter: meeting abstracts published as DOI records in a journal supplement.

Many congresses publish their abstract book as a journal supplement whose publisher deposits each abstract
with Crossref (title, authors, affiliations, page/abstract number and, for some publishers, the abstract
text). Crossref's REST API is a public metadata service intended for machine access, so this adapter can
cover meetings whose own websites forbid automated capture, without touching those websites.

Covered by shipped configs (latest edition on Crossref at build time):
  ASCO Annual, ASCO GI (JCO suppl.), AACR (Cancer Research suppl.), SABCS (Clinical Cancer Research suppl.),
  ASH (Blood suppl.), ASTRO (IJROBP suppl.), WCLC (JTO suppl.), ESMO (Annals of Oncology suppl.),
  SITC (JITC proceedings).

The config's ``source`` block says how to select the meeting's records out of the journal stream:

  "source": {
    "filter": "issn:1527-7755,from-pub-date:2026-05-15,until-pub-date:2026-06-30",   # Crossref filter
    "match": {"doi": "...", "page": "...", "title": "...", "issue": "...", "volume": "..."},  # all regexes must hit
    "number": {"from": "page|doi|title|articleNumber", "regex": "(capture group 1)"},
    "stripTitlePrefix": "regex removed from the title (abstract code prefixes)",
    "formatRules": [["regex on number", "poster|oral|lecture|other", tip?], ...]
  }

Rights: abstract text deposited with Crossref remains the publisher's copyright. Output keeps
``rights.displayFullText = false`` and is for internal analysis only.
"""
from __future__ import annotations

import hashlib
import html as htmllib
import json
import re
import unicodedata
from collections import OrderedDict
from typing import Any

from .base import Adapter
from .http import CaptureBlocked, PoliteHttp, build_user_agent, now

API = "https://api.crossref.org/works"
SELECT = ("DOI,title,author,abstract,page,issue,volume,article-number,published,published-print,"
          "published-online,container-title,type,license,URL,clinical-trial-number,publisher,subject")
ROWS = 500

_WS = re.compile(r"\s+")
_LABELS = [
    "Background and Aims", "Background and Purpose", "Background", "Introduction", "Purpose", "Objectives",
    "Objective", "Aims", "Aim", "Rationale", "Hypothesis", "Materials and Methods", "Patients and Methods",
    "Experimental Design", "Study Design", "Methods", "Method", "Design", "Results", "Result",
    "Conclusions", "Conclusion", "Discussion", "Significance", "Clinical Relevance",
    "Clinical trial information", "Clinical Trial Registration", "Trial Registration", "Research Sponsor",
    "Funding",
]
_LABEL_ALT = "|".join(sorted({x for lab in _LABELS for x in (lab, lab.upper(), lab.title())}, key=len, reverse=True))
# a label starts the text or follows a sentence end / digit / bracket, and is followed by a colon
LABEL_RE = re.compile(r"(?:(?<=^)|(?<=[.!?)\]0-9:;\s]))(" + _LABEL_ALT + r")\s*:\s*")
_DOUBLE_ESCAPED = re.compile(r"&(?:gt|lt|amp|quot|apos|#\d+|#x[0-9a-fA-F]+);")
_FOOTNOTE = re.compile(r"^\s*(?:\d+[,\d]*|[a-z](?=[A-Z]))\s*")

COUNTRIES = {
    "Argentina", "Australia", "Austria", "Belgium", "Brazil", "Bulgaria", "Canada", "Chile", "China",
    "Colombia", "Croatia", "Czech Republic", "Czechia", "Denmark", "Egypt", "Estonia", "Finland", "France",
    "Germany", "Greece", "Hong Kong", "Hungary", "India", "Indonesia", "Iran", "Ireland", "Israel", "Italy",
    "Japan", "Jordan", "Kazakhstan", "Korea", "Republic of Korea", "South Korea", "Lebanon", "Lithuania",
    "Malaysia", "Mexico", "Morocco", "Netherlands", "The Netherlands", "New Zealand", "Nigeria", "Norway",
    "Pakistan", "Peru", "Philippines", "Poland", "Portugal", "Qatar", "Romania", "Russia",
    "Russian Federation", "Saudi Arabia", "Serbia", "Singapore", "Slovakia", "Slovenia", "South Africa",
    "Spain", "Sweden", "Switzerland", "Taiwan", "Thailand", "Tunisia", "Turkey", "Türkiye", "Ukraine",
    "United Arab Emirates", "UAE", "United Kingdom", "UK", "United States", "USA", "Uruguay", "Vietnam",
    "Viet Nam",
}
_COUNTRY_ALIASES = {
    "usa": "United States", "us": "United States", "united states of america": "United States",
    "uk": "United Kingdom", "korea": "South Korea", "republic of korea": "South Korea",
    "korea, republic of": "South Korea", "the netherlands": "Netherlands", "czechia": "Czech Republic",
    "russian federation": "Russia", "türkiye": "Turkey", "viet nam": "Vietnam", "uae": "United Arab Emirates",
    "p.r. china": "China", "pr china": "China", "people's republic of china": "China",
}
_COUNTRY_INDEX = {c.casefold(): _COUNTRY_ALIASES.get(c.casefold(), c) for c in COUNTRIES}
_COUNTRY_INDEX.update(_COUNTRY_ALIASES)
_US_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI", "ID", "IL", "IN", "IA", "KS", "KY",
    "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH",
    "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
}


# ------------------------------------------------------------------------------------------ text
def clean(s: str | None) -> str | None:
    if s is None:
        return None
    s = htmllib.unescape(s).replace("\xa0", " ").replace(" ", " ").replace(" ", " ")
    s = unicodedata.normalize("NFC", s)
    s = _WS.sub(" ", s).strip()
    return s or None


def _markup_to_text(markup: str) -> str:
    from bs4 import BeautifulSoup

    text = BeautifulSoup(markup, "html.parser").get_text(" ")
    if _DOUBLE_ESCAPED.search(text):  # publishers sometimes deposit already-escaped entities
        text = htmllib.unescape(text)
    return text


def jats_abstract(markup: str | None) -> tuple[str | None, dict | None]:
    """JATS abstract -> ('Label: ...' lines, sections dict | None). Text is never paraphrased."""
    if not markup:
        return None, None
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(markup, "html.parser")
    for t in soup.find_all(["jats:title", "title"]):
        label = clean(t.get_text(" "))
        if not label or label.lower() == "abstract":
            t.decompose()
        else:
            t.replace_with(f" {label.rstrip(':')}: ")
    text = soup.get_text(" ")
    if _DOUBLE_ESCAPED.search(text):
        text = htmllib.unescape(text)
    text = clean(text)
    if not text:
        return None, None
    hits = list(LABEL_RE.finditer(text))
    if not hits:
        return text, None
    lines, sections = [], OrderedDict()
    if hits[0].start() > 0:
        lead = clean(text[: hits[0].start()])
        if lead:
            lines.append(lead)
    for i, m in enumerate(hits):
        body = clean(text[m.end(): hits[i + 1].start() if i + 1 < len(hits) else len(text)])
        label = m.group(1).strip()
        if not body:
            continue
        lines.append(f"{label}: {body}")
        from pipeline.parse import section_key

        key = section_key(label)
        sections[key] = (sections[key] + " " + body) if key in sections else body
    return "\n".join(lines), (dict(sections) or None)


def _pub_date(item: dict) -> str | None:
    for key in ("published-online", "published-print", "published"):
        parts = ((item.get(key) or {}).get("date-parts") or [[None]])[0]
        if parts and parts[0]:
            y, m, d = (list(parts) + [1, 1])[:3]
            return f"{int(y):04d}-{int(m or 1):02d}-{int(d or 1):02d}"
    return None


def _split_affiliation(aff: str | None) -> tuple[str | None, str | None, str | None]:
    """'1Dept X, Hospital Y, Paris, France' -> (institution, city, country). Unknown parts stay None."""
    aff = clean(_FOOTNOTE.sub("", aff or "", count=1))
    aff = clean((aff or "").rstrip(" ,;."))
    if not aff:
        return None, None, None
    parts = [p.strip() for p in aff.split(",") if p.strip()]
    country = city = None
    last = parts[-1].rstrip(".").casefold() if parts else ""
    if last in _COUNTRY_INDEX:
        country = _COUNTRY_INDEX[last]
        if len(parts) >= 3:
            city = parts[-2]
    elif len(parts) >= 3 and parts[-1] in _US_STATES:
        country, city = "United States", parts[-2]
    return aff, city, country


# ---------------------------------------------------------------------------------------- adapter
class CrossrefSupplementAdapter(Adapter):
    name = "crossref-supplement-v1"
    platform = "crossref"
    access_basis = ("Crossref REST API (public scholarly-metadata service for machine access; no robots "
                    "Disallow). Conference websites themselves are never contacted.")

    # ---------------------------------------------------------------- config
    @staticmethod
    def source(config) -> dict:
        return config.raw.get("source") or {}

    def validate_config(self, config) -> None:
        from ..config import ConfigurationError

        src = self.source(config)
        if not isinstance(src.get("filter"), str) or "from-pub-date" not in src["filter"]:
            raise ConfigurationError("crossref adapter needs source.filter with a from-pub-date window")
        if not src.get("match"):
            raise ConfigurationError("crossref adapter needs source.match regexes to isolate the meeting's records")
        for field, rx in (src.get("match") or {}).items():
            try:
                re.compile(rx)
            except re.error as exc:
                raise ConfigurationError(f"source.match.{field} is not a valid regex: {exc}") from exc

    def matches(self, item: dict, config) -> bool:
        fields = {
            "doi": item.get("DOI") or "",
            "page": str(item.get("page") or ""),
            "title": clean(_markup_to_text((item.get("title") or [""])[0])) or "",
            "issue": str(item.get("issue") or ""),
            "volume": str(item.get("volume") or ""),
            "type": item.get("type") or "",
            "container": " ".join(item.get("container-title") or []),
        }
        return all(re.search(rx, fields.get(f, ""), re.I) for f, rx in self.source(config)["match"].items())

    # ---------------------------------------------------------------- capture
    def capture(self, config, *, contact=None, delay=1.5, limit=None) -> dict[str, Any]:
        self.validate_config(config)
        src = self.source(config)
        http = PoliteHttp(build_user_agent(contact), delay=max(delay, 1.0))
        bundle = {
            "source": {"baseUrl": API, "platform": self.platform, "adapter": self.name, "userAgent": http.user_agent,
                       "conferenceId": config.conference_id, "filter": src["filter"], "match": src["match"],
                       "started": now()},
            "listings": {}, "details": {}, "errors": http.errors, "inventory": {"records": []},
        }
        cursor, page, seen = "*", 0, 0
        matched: set[str] = set()
        while True:
            params = {"filter": src["filter"], "rows": ROWS, "cursor": cursor, "select": SELECT}
            try:
                r = http.get(API, params)
            except CaptureBlocked as exc:
                bundle["errors"].append([API, str(exc)])
                break
            if r is None:
                bundle["errors"].append([API, f"page {page + 1} failed after retries; capture incomplete"])
                bundle["source"]["incomplete"] = True
                break
            msg = r.json()["message"]
            items = msg.get("items") or []
            page += 1
            seen += len(items)
            page_dois = []
            for item in items:
                if not self.matches(item, config):
                    continue
                doi = item["DOI"].lower()
                if doi in matched:
                    continue
                matched.add(doi)
                page_dois.append(doi)
                body = json.dumps(item, sort_keys=True, ensure_ascii=False)
                bundle["details"][f"crossref:{doi}"] = {"url": f"{API}/{doi}", "retrievedAt": now(),
                                                        "bytes": len(body.encode()), "body": body}
            summary = json.dumps({"page": page, "items": len(items), "matched": sorted(page_dois),
                                  "totalResults": msg.get("total-results")}, sort_keys=True)
            bundle["listings"][f"page_{page:04d}"] = {"url": r.url, "retrievedAt": now(),
                                                      "bytes": len(r.content), "body": summary}
            print(f"[{config.conference_id}] page {page}: {seen}/{msg.get('total-results')} scanned, "
                  f"{len(matched)} matched", flush=True)
            cursor = msg.get("next-cursor")
            if not items or not cursor or (limit and len(matched) >= limit):
                break
        bundle["inventory"]["records"] = sorted(matched)
        bundle["source"].update({"finished": now(), "scanned": seen, "matched": len(matched),
                                 "requests": http.requests})
        bundle["rate"] = {"delay": http.delay, "n429": http.n429}
        return bundle

    # ---------------------------------------------------------------- parse
    def _number(self, item: dict, title: str) -> str | None:
        rule = getattr(self, "_cfg_source", {}).get("number") or {"from": "page"}
        raw = {
            "page": str(item.get("page") or ""),
            "doi": item.get("DOI") or "",
            "title": title,
            "articleNumber": str(item.get("article-number") or ""),
        }.get(rule.get("from", "page"), "")
        if rule.get("regex"):
            m = re.search(rule["regex"], raw, re.I)
            raw = m.group(1) if m else ""
        elif rule.get("from", "page") == "page":
            raw = raw.split("-")[0]
        raw = raw.strip()
        if rule.get("upper"):
            raw = raw.upper()
        return raw or None

    def _format(self, number: str | None) -> tuple[str, bool]:
        for rule in getattr(self, "_cfg_source", {}).get("formatRules") or []:
            rx, fmt = rule[0], rule[1]
            if number and re.search(rx, number, re.I):
                return fmt, bool(rule[2]) if len(rule) > 2 else False
        return "other", False

    def parse(self, bundle, config) -> dict[str, Any]:
        from pipeline import parse as P

        self._cfg_source = self.source(config)
        cid = config.conference_id
        snapshot = (bundle.get("source") or {}).get("finished") or (bundle.get("source") or {}).get("started")
        strip_rx = self._cfg_source.get("stripTitlePrefix")
        presentations, people, failures = [], {}, []
        for key, rec in sorted((bundle.get("details") or {}).items()):
            try:
                item = json.loads(rec["body"])
            except (KeyError, json.JSONDecodeError) as exc:
                failures.append({"key": key, "error": repr(exc)})
                continue
            doi = item["DOI"].lower()
            raw_title = clean(_markup_to_text((item.get("title") or [""])[0]))
            number = self._number(item, raw_title or "")
            title = raw_title
            if strip_rx and title:
                title = clean(re.sub(strip_rx, "", title, count=1)) or title
            fmt, tip_rule = self._format(number)
            abstract, sections = jats_abstract(item.get("abstract"))
            if abstract and number:  # some publishers repeat the abstract number as a heading line
                first, _, rest = abstract.partition("\n")
                if first.strip().casefold() == number.casefold() and rest:
                    abstract = rest
            authors = []
            for order, a in enumerate(item.get("author") or [], start=1):
                name = clean(" ".join(x for x in (a.get("given"), a.get("family")) if x)) or clean(a.get("name"))
                if not name:
                    continue
                affs = [x.get("name") for x in a.get("affiliation") or [] if x.get("name")]
                inst, city, country = _split_affiliation(affs[0] if affs else None)
                author = OrderedDict(
                    personId=P.person_id(name, inst), name=name, degrees=[], pronouns=None,
                    role="author" if a.get("sequence") == "first" or order == 1 else "coauthor",
                    position=None, institution=inst, city=city, region=None, country=country, order=order,
                )
                authors.append(author)
                person = people.setdefault(author["personId"], {"id": author["personId"], "name": name,
                                                                "institutions": set(), "countries": set()})
                if inst:
                    person["institutions"].add(inst)
                if country:
                    person["countries"].add(country)
            pub = _pub_date(item)
            lifts = f"{pub}T00:00:00Z" if pub else None
            p = OrderedDict((f, None) for f in P.PRESENTATION_FIELDS)
            p.update(
                id=f"{cid}:crossref:{doi}", number=number, title=title, titleNormalized=P.normalize_title(title),
                format=fmt, tip=tip_rule or P.detect_tip(title, None, sections), track=None,
                timezone=config.timezone, authors=authors, abstract=abstract, abstractSections=sections,
                disclosures=[], url=f"https://doi.org/{doi}", sourceKind="crossref", sourceId=doi,
                contentHash=hashlib.sha256(rec["body"].encode("utf-8")).hexdigest(),
                embargo={"embargoed": bool(lifts and snapshot and lifts > snapshot), "liftsAtUtc": lifts},
            )
            presentations.append(p)
        presentations.sort(key=lambda x: x["id"])
        people_list = [{"id": v["id"], "name": v["name"], "institutions": sorted(v["institutions"]),
                        "countries": sorted(v["countries"])} for _, v in sorted(people.items())]
        conf = config.raw["conference"]
        conference = OrderedDict(
            id=cid, name=conf["name"], organization=conf.get("organization"), city=conf.get("city"),
            country=conf.get("country"), startDate=conf["startDate"], endDate=conf["endDate"],
            timezone=config.timezone, sourceUrl=conf["sourceUrl"], platform=self.platform,
            snapshot=snapshot,
        )
        report = OrderedDict(
            method="crossref",
            counts=OrderedDict(
                presentations=len(presentations), withAbstract=sum(1 for p in presentations if p["abstract"]),
                posters=sum(1 for p in presentations if p["format"] == "poster"),
                oral=sum(1 for p in presentations if p["format"] == "oral"),
                other=sum(1 for p in presentations if p["format"] == "other"),
                tip=sum(1 for p in presentations if p["tip"]), people=len(people_list),
                scanned=(bundle.get("source") or {}).get("scanned"),
            ),
            discovered=len((bundle.get("inventory") or {}).get("records") or []),
            captured=len(bundle.get("details") or {}),
            fillRates=P.fill_rates(presentations),
            failures=failures,
        )
        return OrderedDict(conference=conference, presentations=presentations, sessions=[], people=people_list,
                           sponsors=[], events=[], parseReport=report)
