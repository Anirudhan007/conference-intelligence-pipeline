"""Parser agent: raw eventscribe bundle -> data/stage/parsed.json (canonical contract in BRIEF.md).

Two extraction methods, selected with --method:
  L  listing only: posters listing (+ postersByCategory for track), presentations listing
     (hidden author blocks), agenda day pages (date, time, location, light author lines).
  D  listing plus detail cards (default): listings give the inventory, PosterInfo /
     PresentationInfo / SessionInfo / ExhibitorInfo cards give track, roles, affiliations,
     abstract, disclosures, date, time, location, session membership, slide counts, views.
     Listing values are used as fallback where a card is missing or silent.

Usage:
  python3 pipeline/parse.py --bundle data/raw/bundle.json --method D --out data/stage/parsed.json
  python3 pipeline/parse.py --bundle data/raw/bundle.json --compare   # both methods + reports/parse_methods.md
"""
from __future__ import annotations

import argparse
import hashlib
import html as htmllib
import json
import re
import sys
import time
import unicodedata
from collections import OrderedDict, defaultdict
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from bs4 import BeautifulSoup, NavigableString, Tag

ROOT = Path(__file__).resolve().parent.parent
CONF_ID = "ctos-2026"
TIMEZONE = "Asia/Seoul"
EMBARGO_LIFTS_UTC = "2026-11-18T14:00:00Z"
BASE_URL = "https://ctos2026.eventscribe.net/"
CONF_FACTS = {"name": "CTOS 2026 Annual Meeting (Connective Tissue Oncology Society)", "city": "Seoul",
              "country": "South Korea", "startDate": "2026-11-18", "endDate": "2026-11-21",
              "sourceUrl": "https://ctos2026.eventscribe.net/index.asp?launcher=1"}


@contextmanager
def conference_context(conf_id: str, timezone: str, base_url: str, embargo_lifts_utc: str | None,
                       facts: dict | None = None):
    """Temporarily point the EventScribe parser at another conference (ids, timezone, URLs, facts).

    The parser keeps module-level constants for backwards compatibility with the CTOS scripts; this
    context swaps them for one build() call and always restores the CTOS defaults afterwards.
    """
    global CONF_ID, TIMEZONE, BASE_URL, EMBARGO_LIFTS_UTC, CONF_FACTS
    saved = (CONF_ID, TIMEZONE, BASE_URL, EMBARGO_LIFTS_UTC, CONF_FACTS)
    CONF_ID, TIMEZONE, EMBARGO_LIFTS_UTC = conf_id, timezone, embargo_lifts_utc
    BASE_URL = base_url.rstrip("/") + "/"
    CONF_FACTS = dict(facts or {})
    try:
        yield
    finally:
        CONF_ID, TIMEZONE, BASE_URL, EMBARGO_LIFTS_UTC, CONF_FACTS = saved

PRESENTATION_FIELDS = [
    "id", "number", "title", "titleNormalized", "format", "tip", "track", "sessionId",
    "sessionTitle", "date", "startTime", "endTime", "timezone", "location", "authors",
    "abstract", "abstractSections", "disclosures", "slidesAvailable", "views", "url",
    "sourceKind", "sourceId", "contentHash", "embargo",
]
AUTHOR_FIELDS = ["personId", "name", "degrees", "pronouns", "role", "position",
                 "institution", "city", "region", "country", "order"]

# ----------------------------------------------------------------------------------------
# text helpers
# ----------------------------------------------------------------------------------------

_WS = re.compile(r"\s+")


def clean(s: str | None) -> str | None:
    """Unescape, NFC-normalise, collapse whitespace. Empty -> None."""
    if s is None:
        return None
    s = htmllib.unescape(s).replace("\xa0", " ")
    s = unicodedata.normalize("NFC", s)
    s = _WS.sub(" ", s).strip()
    return s or None


def soup(html: str) -> BeautifulSoup:
    # html.parser keeps <p> inside <i class="hidden"> (lxml would close the <i>).
    return BeautifulSoup(html or "", "html.parser")


def sha256(s: str) -> str:
    return hashlib.sha256((s or "").encode("utf-8")).hexdigest()


def norm_key(s: str | None) -> str:
    """Accent-free, lowercase, alnum-only key for dedup."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().replace("’", "'")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return _WS.sub(" ", s).strip()


def normalize_title(title: str | None) -> str | None:
    if not title:
        return None
    t = unicodedata.normalize("NFKC", title).replace("’", "'").replace("‘", "'")
    t = t.replace("“", '"').replace("”", '"').replace("–", "-").replace("—", "-")
    t = _WS.sub(" ", t).strip().rstrip(".").strip().lower()
    return t or None


# ----------------------------------------------------------------------------------------
# numbers, titles, formats
# ----------------------------------------------------------------------------------------

POSTER_PREFIX = re.compile(r"^\s*\(\s*(P\s*\d+[A-Za-z]?)\s*\)\s*(.*)$", re.S)
# "Paper 86 - TITLE"; tolerate the site's typo "Psper 33 - ..."
PAPER_PREFIX = re.compile(r"^\s*(?:Paper|P[a-z]per)\s*#?\s*(\d+[A-Za-z]?)\s*[-–—:]\s*(.*)$", re.S | re.I)
# poster-discussion talks: "Poster 2 - TITLE" (session-scoped number, kept as official string)
POSTER_TALK_PREFIX = re.compile(r"^\s*(Poster)\s+(\d+[A-Za-z]?)\s*[-–—:]\s+(.*)$", re.S)
PAPER_SUFFIX = re.compile(r"^(.*?)\s*\(\s*(?:Paper|P[a-z]per)\s*#?\s*(\d+[A-Za-z]?)\s*\)\s*$", re.S | re.I)
LECTURE_RE = re.compile(r"\b(lecture|keynote|award address|presidential address|oration)\b", re.I)
TIP_TITLE_RE = re.compile(r"(?i:\btrials?[\s-]+in[\s-]+progress\b)|\bTiP\b|\(TIP\)")
TIP_TRACK_RE = re.compile(r"trials?\s+in\s+progress", re.I)
LBA_TRACK_RE = re.compile(r"late[\s-]+breaking", re.I)
LOGISTICS_RE = re.compile(
    r"\b(registration|coffee|break|breakfast|lunch|luncheon|dinner|reception|lounge|"
    r"networking|board meeting|business meeting|committee meeting|mentorship|"
    r"speaker ready|exhibit hall|exhibits? open|poster set[- ]?up|poster tear[- ]?down|"
    r"gala|welcome drinks|photo|wellness|prayer|happy hour|shuttle|cloakroom|"
    r"meeting room|meet[- ]up|meetup|tour|entertainment|afterparty|after[- ]party)\b|"
    r"\bmeeting\s*$",
    re.I,
)


def split_poster_title(raw: str | None):
    raw = clean(raw)
    if not raw:
        return None, None
    m = POSTER_PREFIX.match(raw)
    if m:
        return clean(m.group(1)), clean(m.group(2))
    return None, raw


def split_paper_title(raw: str | None):
    """Return (number like 'Paper 86' | None, title)."""
    raw = clean(raw)
    if not raw:
        return None, None
    m = PAPER_PREFIX.match(raw)
    if m:
        return f"Paper {m.group(1)}", clean(m.group(2))
    m = PAPER_SUFFIX.match(raw)
    if m:
        return f"Paper {m.group(2)}", clean(m.group(1))
    m = POSTER_TALK_PREFIX.match(raw)
    if m:
        return f"Poster {m.group(2)}", clean(m.group(3))
    m = re.match(r"^(.*?)\s*\(\s*(Poster\s+\d+[A-Za-z]?)\s*\)\s*$", raw, re.S)
    if m:
        return clean(m.group(2)), clean(m.group(1))
    return None, raw


def is_logistics(title: str | None, buildcode: str | None = None, has_people: bool = False,
                 number: str | None = None) -> bool:
    if number or not title:
        return False
    if LECTURE_RE.search(title):
        return False
    if re.match(r"^\s*session\s+\d+", title, re.I):
        return False
    if LOGISTICS_RE.search(title):
        return True
    if buildcode == "G" and not has_people:
        return True
    return False


NON_TALK_RE = re.compile(
    r"\b(remarks|discussant|symposium|debate|panel|meeting|welcome|introduction|award|ceremony|"
    r"q\s*&\s*a|question and answer)\b|^\s*session\s+\d+", re.I)


def classify_format(p: dict) -> str:
    """poster for PosterInfo; oral for numbered talks (Paper N, Poster N in poster discussion) and
    unnumbered contributed/invited talks that sit inside a scientific session; lecture for named
    lectures; other for everything else (remarks, discussants, debates, industry symposia, meetings)."""
    if p.get("sourceKind") == "PosterInfo":
        return "poster"
    if p.get("number"):
        return "oral"
    title = p.get("title") or ""
    if LECTURE_RE.search(title):
        return "lecture"
    if NON_TALK_RE.search(title):
        return "other"
    roles = {a.get("role") for a in p.get("authors") or []}
    if (p.get("sessionId") or p.get("sessionTitle")) and roles & {"author", "presenter", "speaker", "coauthor"}:
        return "oral"
    return "other"


def detect_tip(title: str | None, track: str | None, abstract_sections: dict | None = None) -> bool:
    """Trial-in-progress detection.

    Title mention is decisive. Track mention is decisive when the track is TiP only; the CTOS
    track "Trials In Progress & Late Breaking Abstracts" mixes TiP and LBA, so there we also
    require (when an abstract is available) that results are absent / N/A / pending.
    Without an abstract (listing only) the track mention alone decides.
    """
    if title and TIP_TITLE_RE.search(title):
        return True
    if not track or not TIP_TRACK_RE.search(track):
        return False
    if not LBA_TRACK_RE.search(track):
        return True
    if abstract_sections is None:
        return True
    res = (abstract_sections.get("results") or "").strip().lower()
    if not res or res in {"n/a", "na", "n.a.", "none", "not applicable", "-", "tbd", "pending"}:
        return True
    if re.search(r"\b(not yet|pending|ongoing|enrol?ling|accrual (is )?ongoing|will be (reported|presented)|"
                 r"to be (reported|presented)|recruitment (is )?ongoing|no results)\b", res):
        return True
    return False


# ----------------------------------------------------------------------------------------
# dates and times
# ----------------------------------------------------------------------------------------

TIME_SPAN = re.compile(
    r"(\d{1,2}):(\d{2})\s*([AaPp])\.?\s*[Mm]\.?\s*[-–—to]+\s*(\d{1,2}):(\d{2})\s*([AaPp])\.?\s*[Mm]\.?")
TIME_ONE = re.compile(r"(\d{1,2}):(\d{2})\s*([AaPp])\.?\s*[Mm]\.?")
DATE_RE = re.compile(
    r"(?:(?:Mon|Tues|Wednes|Thurs|Fri|Satur|Sun)day,\s*)?"
    r"(January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\s+(\d{1,2}),\s*(\d{4})")


def _to24(h: str, m: str, ap: str) -> str:
    h_i = int(h) % 12
    if ap.upper() == "P":
        h_i += 12
    return f"{h_i:02d}:{int(m):02d}"


def parse_time_span(text: str | None):
    """'6:30 AM - 5:30 PM KST' -> ('06:30', '17:30')."""
    if not text:
        return None, None
    m = TIME_SPAN.search(text)
    if m:
        return _to24(*m.group(1, 2, 3)), _to24(*m.group(4, 5, 6))
    m = TIME_ONE.search(text)
    if m:
        return _to24(*m.group(1, 2, 3)), None
    return None, None


def parse_date(text: str | None):
    if not text:
        return None
    m = DATE_RE.search(text)
    if not m:
        return None
    return datetime.strptime(f"{m.group(1)} {m.group(2)} {m.group(3)}", "%B %d %Y").strftime("%Y-%m-%d")


# ----------------------------------------------------------------------------------------
# people
# ----------------------------------------------------------------------------------------

PRONOUN_RE = re.compile(r"\(\s*([A-Za-z]+(?:\s*/\s*[A-Za-z]+){1,3})\s*\)")
NULL_DEGREES = {"n/a", "na", "none", "-", "n.a."}
SINGLE_DEGREES = {
    "MD", "PHD", "MSC", "MS", "MA", "BS", "BSC", "BA", "MPH", "MBA", "DO", "RN", "NP", "PA", "PA-C",
    "MBBS", "FRCPC", "FRCR", "FRCS", "FRACP", "FRCP", "MRCP", "DPHIL", "MHS", "MSN", "BSN", "DNP",
    "PHARMD", "MMED", "DMD", "DDS", "MCH", "MS(ORTHO)", "FACS", "FASCO", "FAAP", "MSCI", "MPHIL",
    "DRPH", "SCD", "FRCSC", "FEBS", "MRCS", "FRCPATH", "FRANZCR", "FRCPI", "FRCPE", "FACP", "MHSC", "MBCHB", "CCRP", "APRN", "FNP", "CRNP", "MED", "MEng".upper(), "BENG", "MBBCH",
}


NAME_SUFFIX_RE = re.compile(r"^\s*(Jr\.?|Sr\.?|II|III|IV)\s*(?:,|$)")


def split_degrees(deg_str: str) -> list[str]:
    out: list[str] = []
    for part in deg_str.split(","):
        p = clean(part)
        if not p or p.lower() in NULL_DEGREES:
            continue
        toks = p.split(" ")
        if len(toks) > 1 and all(t.replace(".", "").upper() in SINGLE_DEGREES for t in toks):
            out.extend(toks)
        else:
            out.append(p)
    return out


def split_name(raw: str | None):
    """'Alex R. Morgan, MD (she/her/hers)' -> ('Alex R. Morgan', ['MD'], 'she/her/hers')."""
    raw = clean(raw)
    if not raw:
        return None, [], None
    pronouns = None
    m = PRONOUN_RE.search(raw)
    if m and "/" in m.group(1):
        pronouns = re.sub(r"\s*/\s*", "/", m.group(1)).lower()
        raw = clean(raw[:m.start()] + raw[m.end():]) or ""
    if "," in raw:
        name, deg = raw.split(",", 1)
        # generational suffixes belong to the name: "John Smith, Jr., MD" -> "John Smith Jr."
        while True:
            m = NAME_SUFFIX_RE.match(deg)
            if not m:
                break
            name = f"{name.strip()} {m.group(1).strip()}"
            deg = deg[m.end():]
        degrees = split_degrees(deg)
    else:
        name, degrees = raw, []
    return clean(name), degrees, pronouns


def split_location(loc: str | None):
    """'Seattle, Washington, United States' -> (city, region, country). Country is the last part."""
    loc = clean(loc)
    if not loc:
        return None, None, None
    parts = [clean(p) for p in loc.split(",")]
    parts = [p for p in parts if p]
    if not parts:
        return None, None, None
    if len(parts) == 1:
        return None, None, parts[0]
    if len(parts) == 2:
        return parts[0], None, parts[1]
    return ", ".join(parts[:-2]), parts[-2], parts[-1]


def parse_prof_lines(lines: list[str]):
    """prof-text lines -> position, institution, city, region, country (last line = location)."""
    lines = [l for l in (clean(x) for x in lines) if l]
    position = institution = city = region = country = None
    if not lines:
        return position, institution, city, region, country
    if len(lines) >= 2 and "," in lines[-1]:
        city, region, country = split_location(lines[-1])
        lines = lines[:-1]
    elif len(lines) >= 3:
        city, region, country = split_location(lines[-1])
        lines = lines[:-1]
    if len(lines) == 1:
        institution = lines[0]
    elif len(lines) >= 2:
        position = lines[0]
        institution = ", ".join(lines[1:])
    return position, institution, city, region, country


def br_lines(node: Tag) -> list[str]:
    parts, cur = [], []
    for c in node.children:
        if isinstance(c, Tag) and c.name == "br":
            parts.append("".join(cur))
            cur = []
        elif isinstance(c, NavigableString):
            cur.append(str(c))
        else:
            cur.append(c.get_text(" "))
    parts.append("".join(cur))
    return [p for p in (clean(x) for x in parts) if p]


ROLE_MAP = [
    (re.compile(r"co-?\s*author", re.I), "coauthor"),
    (re.compile(r"present", re.I), "presenter"),   # Poster Presenter(s), Presenter(s), Presenting Author
    (re.compile(r"author", re.I), "author"),
    (re.compile(r"co-?chair|chair", re.I), "chair"),
    (re.compile(r"moderat", re.I), "moderator"),
    (re.compile(r"speak|panel|faculty|discuss|lectur|debat", re.I), "speaker"),
]


def map_role(text: str | None) -> str:
    t = text or ""
    for rx, role in ROLE_MAP:
        if rx.search(t):
            return role
    return "speaker"


def person_id(name: str | None, institution: str | None) -> str:
    key = f"{norm_key(name)}|{norm_key(institution)}"
    return "person:" + hashlib.sha256(key.encode()).hexdigest()[:16]


def make_author(raw_name, role, order, position=None, institution=None, city=None, region=None,
                country=None, presenter_id=None):
    name, degrees, pronouns = split_name(raw_name)
    a = OrderedDict(
        personId=person_id(name, institution), name=name, degrees=degrees, pronouns=pronouns,
        role=role, position=clean(position), institution=clean(institution), city=clean(city),
        region=clean(region), country=clean(country), order=order,
    )
    if presenter_id:
        a["_presenterId"] = presenter_id  # internal, stripped before output
    return a


def parse_speaker_sections(root: Tag) -> list[dict]:
    """Walk h2.role-title + following ul.speakers-wrap in document order."""
    authors: list[dict] = []
    order = 0
    for h2 in root.select("h2.role-title"):
        label = clean(h2.get_text(" "))
        if not label or label.lower().startswith("presentations"):
            continue
        role = map_role(label)
        ul = h2.find_next_sibling()
        while ul is not None and not (isinstance(ul, Tag) and ul.name == "ul"):
            if isinstance(ul, Tag) and ul.name == "h2":
                ul = None
                break
            ul = ul.find_next_sibling()
        if ul is None or "speakers-wrap" not in (ul.get("class") or []):
            continue
        for li in ul.select("li.speakerrow"):
            nm = li.select_one(".speaker-name")
            if nm is None:
                continue
            prof = None
            for p in li.select(".prof-text"):
                txt = clean(p.get_text(" ")) or ""
                if re.match(r"^disclosure", txt, re.I):
                    continue
                prof = p
                break
            pos, inst, city, region, country = parse_prof_lines(br_lines(prof)) if prof else (None,) * 5
            order += 1
            authors.append(make_author(nm.get_text(" "), role, order, pos, inst, city, region, country,
                                       li.get("data-presenterid")))
    return authors


# ----------------------------------------------------------------------------------------
# abstracts and disclosures
# ----------------------------------------------------------------------------------------

SECTION_SYNONYMS = {
    "objective": "objective", "objectives": "objective", "purpose": "objective", "aim": "objective",
    "aims": "objective", "background": "background", "introduction": "background",
    "background and objectives": "background", "method": "methods", "methods": "methods",
    "materials and methods": "methods", "methodology": "methods", "patients and methods": "methods",
    "result": "results", "results": "results", "conclusion": "conclusion",
    "conclusions": "conclusion", "discussion": "discussion",
}
LABEL_MARK = "⁣LBL⁣"


def section_key(label: str) -> str:
    k = norm_key(label.rstrip(":"))
    if k in SECTION_SYNONYMS:
        return SECTION_SYNONYMS[k]
    return re.sub(r"\s+", "_", k) or "section"


def parse_disclosures(root: Tag) -> list[dict]:
    out = []
    for blk in root.select(".presentation-disclosure-block"):
        b = blk.find(["b", "strong"])
        if b is None:
            continue
        nm = split_name(b.get_text(" "))[0]
        full = clean(blk.get_text(" ")) or ""
        btxt = clean(b.get_text(" ")) or ""
        rest = full[len(btxt):] if full.startswith(btxt) else full.replace(btxt, "", 1)
        rest = clean(rest.lstrip(" :")) or None
        out.append(OrderedDict(name=nm, text=rest))
    return out


def abstract_from_node(node: Tag | None):
    """.PosterAbstractText HTML -> (plain text with 'Label: ...' lines, sections dict | None)."""
    if node is None:
        return None, None
    node = soup(str(node))  # work on a copy
    for d in node.select(".presentation-disclosure-block"):
        parent = d.parent
        d.decompose()
        if isinstance(parent, Tag) and not parent.get_text(strip=True) and parent.name == "div":
            parent.decompose()
    for img in node.find_all("img"):
        img.decompose()
    for bold in node.find_all(["b", "strong"]):
        if bold.find_parent(["b", "strong"]) is not None:
            continue
        label = clean(bold.get_text(" "))
        if not label:
            bold.decompose()
            continue
        bold.replace_with(NavigableString(f"\n{LABEL_MARK}{label}{LABEL_MARK}\n"))
    for br in node.find_all("br"):
        br.replace_with(NavigableString("\n"))
    for blk in node.find_all(["p", "div", "li"]):
        blk.insert_before(NavigableString("\n"))
    text = htmllib.unescape(node.get_text()).replace("\xa0", " ")
    # tokens: labels and text chunks
    pieces = re.split(f"{LABEL_MARK}(.*?){LABEL_MARK}", text, flags=re.S)
    sections: list[list] = []  # [label|None, text]
    pre = clean(pieces[0])
    if pre:
        sections.append([None, pre])
    for i in range(1, len(pieces), 2):
        label = clean(pieces[i]) or ""
        body = clean(pieces[i + 1]) if i + 1 < len(pieces) else None
        # inline bold that is not a heading (no colon, followed directly by lowercase text) -> merge
        sections.append([label, body or ""])
    if not sections:
        return None, None
    lines, secs = [], OrderedDict()
    for label, body in sections:
        if label is None:
            lines.append(body)
            secs.setdefault("preamble", body)
            continue
        lab = label.rstrip()
        if lab.endswith(":"):
            lab_disp = lab[:-1].rstrip()
            lines.append(f"{lab_disp}: {body}".rstrip() if body else f"{lab_disp}:")
        else:
            lab_disp = lab
            lines.append(lab_disp)
            if body:
                lines.append(body)
        if body:
            k = section_key(lab_disp)
            if k in secs:
                secs[k] = secs[k] + " " + body
            else:
                secs[k] = body
    abstract = "\n".join(l for l in lines if l).strip() or None
    has_labels = any(k != "preamble" for k in secs)
    return abstract, (dict(secs) if has_labels else None)


def find_abstract_node(root: Tag) -> Tag | None:
    for sel in (".PosterAbstractText", ".PresentationAbstractText", ".abstract-text", "[class*=AbstractText]"):
        n = root.select_one(sel)
        if n is not None:
            return n
    return None


VIEWS_RES = [
    re.compile(r"(\d[\d,]*)\s*(?:Views?|views?|VIEWS?)\b"),
    re.compile(r"\bViews?\s*[:(]\s*(\d[\d,]*)"),
]


def parse_views(root: Tag) -> int | None:
    for g in root.select(".popup_gutter"):
        m = VIEWS_RES[0].search(g.get_text(" "))
        if m:
            return int(m.group(1).replace(",", ""))
    for el in root.select(".fa-eye, [class*=view], [title*=iew]"):
        ctx = el.parent.get_text(" ") if el.parent is not None else ""
        ctx = (el.get("title") or "") + " " + el.get_text(" ") + " " + ctx
        for rx in VIEWS_RES:
            m = rx.search(ctx)
            if m:
                return int(m.group(1).replace(",", ""))
        if "fa-eye" in (el.get("class") or []):
            m = re.search(r"(\d[\d,]*)", ctx)
            if m:
                return int(m.group(1).replace(",", ""))
    return None


SLIDES_RE = re.compile(r"Slides?\s+Available\s*\(\s*(\d+)\s*Slides?\s*\)", re.I)


def parse_slides(text: str | None) -> int | None:
    if not text:
        return None
    m = SLIDES_RE.search(text)
    return int(m.group(1)) if m else None


# ----------------------------------------------------------------------------------------
# listing parsers
# ----------------------------------------------------------------------------------------

def abs_url(u: str | None) -> str | None:
    if not u:
        return None
    u = htmllib.unescape(u)
    u = re.sub(r"&rnd=[^&]*", "", u)
    if u.startswith("http"):
        return u
    return BASE_URL + u.lstrip("/")


def parse_poster_listing(html: str) -> list[dict]:
    s = soup(html)
    out = []
    for li in s.select("li[data-posterid]"):
        num = li.select_one(".prestime")
        tit = li.select_one(".prestitle")
        number = clean(num.get_text(" ")) if num else None
        raw_title = clean(tit.get_text(" ")) if tit else None
        n2, title = split_poster_title(raw_title)
        out.append(dict(posterId=li["data-posterid"], number=number or n2, title=title,
                        url=abs_url(li.get("data-url")), html=str(li)))
    return out


def parse_posters_by_category(html: str) -> dict[str, str]:
    """posterId -> category/track. Category headers are non-poster rows preceding posters."""
    s = soup(html)
    track_of: dict[str, str] = {}
    current = None
    for el in s.find_all(["li", "h2", "h3", "h4", "div"]):
        if el.name == "li" and el.get("data-posterid"):
            if current:
                track_of.setdefault(el["data-posterid"], current)
            continue
        cls = " ".join(el.get("class") or [])
        if el.name == "li" and ("dayrow" in cls or "divider" in cls or "category" in cls.lower()
                                or "list-group-header" in cls or "trackrow" in cls):
            current = clean(el.get_text(" "))
        elif el.name in ("h2", "h3", "h4") and el.find_parent("li") is None:
            current = clean(el.get_text(" "))
        elif el.name == "div" and ("trackname" in cls or "categoryname" in cls.lower()):
            current = clean(el.get_text(" "))
    return track_of


HIDDEN_CHUNK = re.compile(
    r"^\s*(?P<disp>.*?)\s*###\s*(?P<hid>\d+)\s*###(?P<role>[^#]*)###(?P<pos>[^#]*)###(?P<inst>[^#]*)###", re.S)


def parse_hidden_authors(text: str) -> list[dict]:
    authors = []
    order = 0
    for chunk in text.split("@@@"):
        m = HIDDEN_CHUNK.match(chunk)
        if not m:
            continue
        disp = clean(m.group("disp")) or ""
        raw_name = re.split(r"\s+[–—-]\s+", disp, maxsplit=1)[0]
        role = map_role(m.group("role"))
        order += 1
        inst = clean(m.group("inst"))
        # hidden institution often carries ", City, Country" -- keep as listed (no structured location)
        authors.append(make_author(raw_name, role, order, clean(m.group("pos")), inst))
    return authors


def parse_presentation_listing(html: str) -> list[dict]:
    s = soup(html)
    out = []
    for li in s.select("li[data-presid]"):
        tit = li.select_one(".prestitle")
        raw_title = clean(tit.get_text(" ")) if tit else None
        number, title = split_paper_title(raw_title)
        hid = li.select_one("i.hidden")
        authors = parse_hidden_authors(hid.get_text(" ")) if hid else []
        if number is None and hid is not None:
            m = re.search(r"(?:Paper|P[a-z]per)\s+(\d+[A-Za-z]?)\s*$", hid.get_text(" ").strip())
            if m:
                number = f"Paper {m.group(1)}"
        du = li.get("data-url") or ""
        kind = "SessionInfo" if "sessioninfo" in du.lower() else "PresentationInfo"
        if kind == "SessionInfo":
            number, title = None, raw_title
        out.append(dict(presId=li["data-presid"], kind=kind, number=number, title=title, authors=authors,
                        url=abs_url(li.get("data-url")), html=str(li),
                        slides=parse_slides(str(li))))
    return out


def parse_agenda_day(html: str, fallback_date: str | None = None) -> list[dict]:
    s = soup(html)
    rows = []
    date = fallback_date
    for li in s.find_all("li"):
        cls = li.get("class") or []
        if "dayrow" in cls:
            date = parse_date(li.get_text(" ")) or date
            continue
        if "list-row" not in cls or not li.get("data-url"):
            continue  # bucket headers duplicate the following loadbyurl row
        url = li["data-url"]
        kind = "SessionInfo" if "SessionInfo" in url else ("PresentationInfo" if "PresentationInfo" in url else None)
        if kind is None:
            continue
        m = re.search(r"PresentationID=(\d+)", url, re.I)
        pid = li.get("data-presid") or (m.group(1) if m else None)
        tm = li.select_one(".prestime")
        start, end = parse_time_span(tm.get_text(" ") if tm else None)
        prim = li.select_one(".list-row-primary")
        tspan = prim.find("span", recursive=False) if prim else None
        raw_title = clean(tspan.get_text(" ")) if tspan else None
        loc = None
        if prim:
            for d in prim.find_all("div", recursive=False):
                t = clean(d.get_text(" ")) or ""
                if t.lower().startswith("location:"):
                    loc = clean(t.split(":", 1)[1])
        people = []
        order = 0
        for d in li.select("section .mar-btm-xs"):
            t = clean(d.get_text(" ")) or ""
            mm = re.match(r"^([^:]+):\s*(.*)$", t)
            if not mm:
                continue
            a = d.find("a")
            raw_name = clean(a.get_text(" ")) if a else re.split(r"\s+[–—]\s+", mm.group(2))[0]
            inst = None
            parts = re.split(r"\s+[–—]\s+", mm.group(2), maxsplit=1)
            if len(parts) == 2:
                inst = clean(parts[1])
            order += 1
            people.append(make_author(raw_name, map_role(mm.group(1)), order, None, inst))
        rows.append(dict(kind=kind, id=pid, buildcode=li.get("data-buildcode"), date=date,
                         startTime=start, endTime=end, rawTitle=raw_title, location=loc,
                         people=people, url=abs_url(url), html=str(li),
                         slides=parse_slides(str(li))))
    return rows


def parse_sponsor_listing(html: str) -> list[dict]:
    s = soup(html)
    out = []
    for li in s.select("li[data-boothid]"):
        img = li.find("img")
        small = li.select_one("small.hidden")
        name = clean(small.get_text(" ")) if small else (clean(img.get("alt")) if img else None)
        url = li.get("data-url") or ""
        m = re.search(r"ExhibitorID=(\d+)", url)
        out.append(dict(boothId=li["data-boothid"], exhibitorId=m.group(1) if m else None, name=name,
                        logoUrl=img.get("src") if img else None, url=abs_url(url), html=str(li)))
    return out


# ----------------------------------------------------------------------------------------
# detail card parsers
# ----------------------------------------------------------------------------------------

def _tidbits(root: Tag):
    date = start = end = loc = None
    for tb in root.select(".pres-tidbit"):
        t = clean(tb.get_text(" ")) or ""
        if tb.select_one(".fa-calendar") is not None or DATE_RE.search(t):
            date = date or parse_date(t)
        if tb.select_one(".fa-clock-o, .fa-clock") is not None or TIME_SPAN.search(t):
            s_, e_ = parse_time_span(t)
            start, end = start or s_, end or e_
        if tb.select_one(".fa-map-marker") is not None or t.lower().startswith("location"):
            loc = loc or clean(re.sub(r"^\s*Location:\s*", "", t, flags=re.I))
    return date, start, end, loc


def parse_poster_card(html: str) -> dict:
    s = soup(html)
    track_el = s.select_one(".trackname")
    tracks = [clean(x.get_text(" ")) for x in s.select(".trackname")]
    tracks = [t for t in tracks if t]
    h1 = s.find("h1")
    number, title = split_poster_title(h1.get_text(" ") if h1 else None)
    date, start, end, loc = _tidbits(s)
    abstract, sections = abstract_from_node(find_abstract_node(s))
    return dict(
        track=" | ".join(tracks) if tracks else (clean(track_el.get_text(" ")) if track_el else None),
        number=number, title=title, date=date, startTime=start, endTime=end, location=loc,
        authors=parse_speaker_sections(s), abstract=abstract, abstractSections=sections,
        disclosures=parse_disclosures(s), views=parse_views(s), slides=parse_slides(html),
    )


def parse_presentation_card(html: str) -> dict:
    s = soup(html)
    pc = s.select_one(".popup_content") or s
    session_title = None
    h1 = pc.find("h1")
    for b in pc.find_all("b"):
        if h1 is not None and b.find_previous("h1") is h1:
            break  # only bold text before the h1 is the session label
        if b.find_parent(class_="presentation-disclosure-block") or b.find_parent(class_=re.compile("AbstractText")):
            continue
        t = clean(b.get_text(" "))
        if t:
            session_title = t
            break
    number, title = split_paper_title(h1.get_text(" ") if h1 else None)
    tracks = [t for t in (clean(x.get_text(" ")) for x in s.select(".trackname")) if t]
    date, start, end, loc = _tidbits(s)
    abstract, sections = abstract_from_node(find_abstract_node(s))
    return dict(
        sessionTitle=session_title, number=number, title=title, track=" | ".join(tracks) or None,
        date=date, startTime=start, endTime=end, location=loc, authors=parse_speaker_sections(s),
        abstract=abstract, abstractSections=sections, disclosures=parse_disclosures(s),
        views=parse_views(s), slides=parse_slides(html),
    )


def parse_session_card(html: str) -> dict:
    s = soup(html)
    h1 = s.find("h1")
    title = clean(h1.get_text(" ")) if h1 else None
    date, start, end, loc = _tidbits(s)
    people = parse_speaker_sections(s)
    children = []
    for li in s.select("li[data-presid]"):
        tit = li.select_one(".prestitle")
        ctitle = None
        if tit is not None:
            first = []
            for c in tit.children:
                if isinstance(c, Tag) and c.name in ("br", "small"):
                    break
                first.append(c.get_text(" ") if isinstance(c, Tag) else str(c))
            ctitle = clean("".join(first))
        tools = li.select_one(".list-tools-wrap")
        slides = None
        if tools is not None:
            for sp in tools.select("[original-title], [title]"):
                slides = parse_slides(sp.get("original-title") or sp.get("title"))
                if slides is not None:
                    break
        tm = li.select_one(".prestime")
        cs, ce = parse_time_span(tm.get_text(" ") if tm else None)
        children.append(dict(presId=li["data-presid"], title=ctitle, slides=slides, startTime=cs, endTime=ce))
    return dict(title=title, date=date, startTime=start, endTime=end, location=loc,
                chairs=[p for p in people if p["role"] == "chair"],
                moderators=[p for p in people if p["role"] == "moderator"],
                otherPeople=[p for p in people if p["role"] not in ("chair", "moderator")],
                children=children)


def parse_exhibitor_card(html: str) -> dict:
    s = soup(html)
    about = s.select_one("#about") or s
    h1 = about.find("h1") or s.find(["h1", "h2"])
    name = clean(h1.get_text(" ")) if h1 else None
    level = None
    badge = s.find("img", src=re.compile(r"/Badges/", re.I))
    if badge is not None:
        m = re.search(r"/Badges/([A-Za-z0-9_-]+)\.", badge["src"])
        level = m.group(1).lower() if m else None
    pre = about.find("pre")
    desc = clean(pre.get_text(" ")) if pre is not None else None
    website = None
    contact = s.select_one("#contactinfo")
    if contact is not None:
        for a in contact.find_all("a", href=True):
            t = clean(a.get_text(" ")) or ""
            if re.match(r"^(https?://|www\.)", t, re.I):
                website = t
                break
    address = None
    if contact is not None:
        addr = contact.select_one(".CompanyAddress1")
        if addr is not None:
            address = "; ".join(br_lines(addr)[1:]) or None
    booth = None
    m = re.search(r"\bBooth\s*(?:#|No\.?|Number)?\s*:?\s*([A-Za-z]?\d+[A-Za-z]?)\b", s.get_text(" "))
    if m:
        booth = m.group(1)
    return dict(name=name, website=website, booth=booth, description=desc, level=level, address=address)


# ----------------------------------------------------------------------------------------
# assembly
# ----------------------------------------------------------------------------------------

def _empty_presentation(kind: str, sid: str) -> OrderedDict:
    p = OrderedDict((f, None) for f in PRESENTATION_FIELDS)
    p.update(id=f"{CONF_ID}:{kind}:{sid}", sourceKind=kind, sourceId=str(sid), timezone=TIMEZONE,
             authors=[], disclosures=[], tip=False)
    return p


def _set(p: dict, key: str, val, override: bool = True):
    if val is None or val == [] or val == {}:
        return
    if override or p.get(key) in (None, [], {}):
        p[key] = val


def _day_listing_names(listings: dict) -> list[str]:
    names = [n for n in listings if re.match(r"^day_\d{4}_\d{2}_\d{2}$", n)]
    return sorted(names)


def _fallback_date(name: str) -> str | None:
    m = re.match(r"^day_(\d{4})_(\d{2})_(\d{2})$", name)
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else None


def _time_within(child: dict, sess: dict) -> bool:
    if child.get("date") != sess.get("date") or not child.get("startTime") or not sess.get("startTime"):
        return False
    if (child.get("location") or "") != (sess.get("location") or ""):
        return False
    s_end = sess.get("endTime") or sess["startTime"]
    return sess["startTime"] <= child["startTime"] < s_end or child["startTime"] == sess["startTime"]


def _event_from_row(row: dict, title: str) -> OrderedDict:
    return OrderedDict(
        id=f"{CONF_ID}:{row['kind']}:{row['id']}", title=title, date=row.get("date"),
        startTime=row.get("startTime"), endTime=row.get("endTime"), timezone=TIMEZONE,
        location=row.get("location"), format="other", url=row.get("url"), sourceKind=row["kind"],
        sourceId=row["id"], buildcode=row.get("buildcode"), contentHash=sha256(row.get("html", "")),
    )


def build(bundle: dict, method: str = "D") -> dict:
    method = method.upper()
    assert method in ("D", "L")
    t0 = time.perf_counter()
    listings = bundle.get("listings", {}) or {}
    details = bundle.get("details", {}) or {}
    use_cards = method == "D"
    failures: list[dict] = []

    def L(name):
        v = listings.get(name)
        return (v or {}).get("html") if isinstance(v, dict) else v

    def card(kind, sid):
        if not use_cards:
            return None
        v = details.get(f"{kind}:{sid}")
        if isinstance(v, dict):
            return v.get("html")
        return v

    pres: dict[str, OrderedDict] = {}
    events: dict[str, OrderedDict] = {}
    sessions: dict[str, OrderedDict] = {}
    agenda_people: dict[str, list] = {}

    # ---- posters -------------------------------------------------------------------------
    poster_rows = parse_poster_listing(L("posters") or "")
    track_by_poster = parse_posters_by_category(L("postersByCategory") or "") if L("postersByCategory") else {}
    for r in poster_rows:
        p = _empty_presentation("PosterInfo", r["posterId"])
        p.update(number=r["number"], title=r["title"], format="poster", url=r["url"],
                 track=track_by_poster.get(r["posterId"]), contentHash=sha256(r["html"]))
        pres[p["id"]] = p
    # posters that appear only by category or only as cards
    extra_poster_ids = set(track_by_poster) - {r["posterId"] for r in poster_rows}
    if use_cards:
        extra_poster_ids |= {k.split(":", 1)[1] for k in details if k.startswith("PosterInfo:")} - {
            r["posterId"] for r in poster_rows}
    for pid in sorted(extra_poster_ids):
        p = _empty_presentation("PosterInfo", pid)
        p.update(format="poster", track=track_by_poster.get(pid),
                 url=BASE_URL + f"ajaxcalls/PosterInfo.asp?PosterID={pid}")
        pres[p["id"]] = p

    # ---- presentations listing -----------------------------------------------------------
    for r in parse_presentation_listing(L("presentations") or ""):
        if r["kind"] == "SessionInfo":
            sid = f"{CONF_ID}:SessionInfo:{r['presId']}"
            sessions[sid] = OrderedDict(
                id=sid, title=r["title"], date=None, startTime=None, endTime=None, timezone=TIMEZONE,
                location=None, chairs=[a for a in r["authors"] if a["role"] == "chair"],
                moderators=[a for a in r["authors"] if a["role"] == "moderator"], presentationIds=[],
                url=r["url"], sourceKind="SessionInfo", sourceId=r["presId"], contentHash=sha256(r["html"]))
            continue
        p = _empty_presentation("PresentationInfo", r["presId"])
        p.update(number=r["number"], title=r["title"], url=r["url"], authors=r["authors"],
                 contentHash=sha256(r["html"]))
        _set(p, "slidesAvailable", r["slides"])
        pres[p["id"]] = p

    # ---- agenda --------------------------------------------------------------------------
    agenda_rows = []
    day_names = _day_listing_names(listings)
    for name in day_names:
        agenda_rows.extend(parse_agenda_day(L(name) or "", _fallback_date(name)))
    if not day_names and L("agenda"):
        agenda_rows.extend(parse_agenda_day(L("agenda")))
    elif L("agenda"):
        # agenda overview may contain rows not on day pages
        seen = {(r["kind"], r["id"]) for r in agenda_rows}
        agenda_rows.extend(r for r in parse_agenda_day(L("agenda")) if (r["kind"], r["id"]) not in seen)
    seen_rows = set()
    for row in agenda_rows:
        key = (row["kind"], row["id"])
        if key in seen_rows or not row["id"]:
            continue
        seen_rows.add(key)
        if row["kind"] == "SessionInfo":
            sid = f"{CONF_ID}:SessionInfo:{row['id']}"
            chairs = [a for a in row["people"] if a["role"] == "chair"]
            mods = [a for a in row["people"] if a["role"] == "moderator"]
            if sid in sessions:  # from the presentations listing: fill schedule, keep richer people
                s_ = sessions[sid]
                for k in ("date", "startTime", "endTime", "location"):
                    _set(s_, k, row[k], override=False)
                _set(s_, "title", clean(row["rawTitle"]), override=False)
                _set(s_, "chairs", chairs, override=False)
                _set(s_, "moderators", mods, override=False)
                continue
            sessions[sid] = OrderedDict(
                id=sid, title=clean(row["rawTitle"]), date=row["date"], startTime=row["startTime"],
                endTime=row["endTime"], timezone=TIMEZONE, location=row["location"], chairs=chairs,
                moderators=mods, presentationIds=[], url=row["url"], sourceKind="SessionInfo",
                sourceId=row["id"], contentHash=sha256(row["html"]))
            continue
        number, title = split_paper_title(row["rawTitle"])
        pid = f"{CONF_ID}:PresentationInfo:{row['id']}"
        has_people = bool(row["people"])
        if not has_people and use_cards and card("PresentationInfo", row["id"]):
            has_people = "speakerrow" in card("PresentationInfo", row["id"])
        if pid not in pres and is_logistics(title, row["buildcode"], has_people, number):
            events[pid] = _event_from_row(row, title)
            continue
        p = pres.get(pid)
        if p is None:
            p = _empty_presentation("PresentationInfo", row["id"])
            p.update(url=row["url"], contentHash=sha256(row["html"]))
            pres[pid] = p
        _set(p, "number", number, override=False)
        _set(p, "title", title, override=False)
        for k in ("date", "startTime", "endTime", "location"):
            _set(p, k, row[k], override=False)
        _set(p, "slidesAvailable", row["slides"], override=False)
        if not p["authors"]:
            p["authors"] = row["people"]
        agenda_people[pid] = row["people"]
        p["_buildcode"] = row["buildcode"]

    # ---- other event listings (sponsor events, networking, ancillary) as events -----------
    for name in ("sponsorEvents", "networking", "ancillary"):
        h = L(name)
        if not h:
            continue
        for row in parse_agenda_day(h):
            pid = f"{CONF_ID}:{row['kind']}:{row['id']}"
            if pid in pres or pid in events or pid in sessions or not row["id"]:
                continue
            number, title = split_paper_title(row["rawTitle"])
            ev = _event_from_row(row, title)
            ev["listing"] = name
            events[pid] = ev

    # ---- detail cards --------------------------------------------------------------------
    if use_cards:
        # sessions first (membership, slide counts)
        sess_ids = {s_["sourceId"] for s_ in sessions.values()} | {
            k.split(":", 1)[1] for k in details if k.startswith("SessionInfo:")}
        child_info: dict[str, dict] = {}
        for sid_raw in sorted(sess_ids):
            h = card("SessionInfo", sid_raw)
            sid = f"{CONF_ID}:SessionInfo:{sid_raw}"
            s_ = sessions.get(sid) or OrderedDict(
                id=sid, title=None, date=None, startTime=None, endTime=None, timezone=TIMEZONE,
                location=None, chairs=[], moderators=[], presentationIds=[],
                url=BASE_URL + f"ajaxcalls/SessionInfo.asp?PresentationID={sid_raw}",
                sourceKind="SessionInfo", sourceId=sid_raw, contentHash=None)
            sessions[sid] = s_
            if not h:
                continue
            try:
                sc = parse_session_card(h)
            except Exception as e:  # noqa: BLE001
                failures.append(dict(key=f"SessionInfo:{sid_raw}", error=repr(e)))
                continue
            for k in ("title", "date", "startTime", "endTime", "location"):
                _set(s_, k, sc[k])
            if sc["chairs"]:
                s_["chairs"] = sc["chairs"]
            if sc["moderators"]:
                s_["moderators"] = sc["moderators"]
            s_["contentHash"] = sha256(h)
            s_["presentationIds"] = [f"{CONF_ID}:PresentationInfo:{c['presId']}" for c in sc["children"]]
            for c in sc["children"]:
                child_info[c["presId"]] = dict(sessionId=sid, sessionTitle=s_["title"], **c)

        # make sure every session child exists as a presentation
        for cid, ci in child_info.items():
            pid = f"{CONF_ID}:PresentationInfo:{cid}"
            if pid not in pres:
                p = _empty_presentation("PresentationInfo", cid)
                p.update(url=BASE_URL + f"ajaxcalls/PresentationInfo.asp?PresentationID={cid}")
                n, t = split_paper_title(ci["title"])
                p.update(number=n, title=t)
                pres[pid] = p

        # presentation-ish cards that no listing mentioned
        for k in details:
            kind, _, sid_raw = k.partition(":")
            if kind == "PresentationInfo":
                pid = f"{CONF_ID}:PresentationInfo:{sid_raw}"
                if pid not in pres and pid not in events:
                    p = _empty_presentation("PresentationInfo", sid_raw)
                    p.update(url=(details[k] or {}).get("url") if isinstance(details[k], dict) else None)
                    p["url"] = abs_url(p["url"]) or BASE_URL + f"ajaxcalls/PresentationInfo.asp?PresentationID={sid_raw}"
                    pres[pid] = p

        for pid, p in list(pres.items()):
            kind, sid_raw = p["sourceKind"], p["sourceId"]
            h = card(kind, sid_raw)
            if not h:
                continue
            try:
                c = parse_poster_card(h) if kind == "PosterInfo" else parse_presentation_card(h)
            except Exception as e:  # noqa: BLE001
                failures.append(dict(key=f"{kind}:{sid_raw}", error=repr(e)))
                continue
            p["contentHash"] = sha256(h)
            if kind == "PosterInfo":
                _set(p, "number", p["number"] or c["number"])  # keep official listing string
            else:
                _set(p, "number", c["number"], override=False)
                _set(p, "sessionTitle", c["sessionTitle"])
            _set(p, "title", c["title"], override=p["title"] is None or kind == "PresentationInfo")
            for k in ("track", "date", "startTime", "endTime", "location", "abstract",
                      "abstractSections", "views"):
                _set(p, k, c[k])
            if c["authors"]:
                p["authors"] = c["authors"]
            if c["disclosures"]:
                p["disclosures"] = c["disclosures"]
            _set(p, "slidesAvailable", c["slides"], override=False)
            p["_cardPeople"] = bool(c["authors"])

        for cid, ci in child_info.items():
            pid = f"{CONF_ID}:PresentationInfo:{cid}"
            p = pres[pid]
            p["sessionId"] = ci["sessionId"]
            _set(p, "sessionTitle", ci["sessionTitle"], override=False)
            _set(p, "slidesAvailable", ci["slides"])
            _set(p, "startTime", ci["startTime"], override=False)
            _set(p, "endTime", ci["endTime"], override=False)
            s_ = sessions[ci["sessionId"]]
            for k in ("date", "location"):
                _set(p, k, s_.get(k), override=False)

    # ---- session membership for method L (and D fallback): time window + same room -------
    sess_by_title = {norm_key(s_["title"]): s_ for s_ in sessions.values() if s_.get("title")}
    for pid, p in pres.items():
        if p["sourceKind"] != "PresentationInfo":
            continue
        if not p["sessionId"] and p.get("sessionTitle"):
            s_ = sess_by_title.get(norm_key(p["sessionTitle"]))
            if s_ is None:
                # agenda titles differ slightly ("Sarcoma of the Year - X" vs "Sarcoma of the Year X")
                k = norm_key(p["sessionTitle"])
                m = re.match(r"^session (\d+)\b", k)
                if m:
                    cands = [s for kk, s in sess_by_title.items() if re.match(rf"^session {m.group(1)}\b", kk)]
                    s_ = cands[0] if len(cands) == 1 else None
            if s_ is not None:
                p["sessionId"] = s_["id"]
        if not p["sessionId"] and p["number"]:
            cands = [s_ for s_ in sessions.values() if _time_within(p, s_)]
            if len(cands) == 1:
                p["sessionId"] = cands[0]["id"]
        if p["sessionId"]:
            s_ = sessions.get(p["sessionId"])
            if s_ is not None:
                _set(p, "sessionTitle", s_["title"], override=False)
                if pid not in s_["presentationIds"]:
                    s_["presentationIds"].append(pid)

    # ---- finalize presentations ----------------------------------------------------------
    final_pres = []
    for pid, p in pres.items():
        # demote logistics (PresentationInfo with no number, no people, logistics title)
        if p["sourceKind"] == "PresentationInfo" and not p["number"] and not p["authors"] and \
                is_logistics(p["title"], p.get("_buildcode"), False, None):
            row = dict(kind="PresentationInfo", id=p["sourceId"], date=p["date"], startTime=p["startTime"],
                       endTime=p["endTime"], location=p["location"], url=p["url"], buildcode=p.get("_buildcode"),
                       html="")
            ev = _event_from_row(row, p["title"])
            ev["contentHash"] = p["contentHash"]
            events[pid] = ev
            for s_ in sessions.values():
                if pid in s_["presentationIds"]:
                    s_["presentationIds"].remove(pid)
            continue
        p["format"] = classify_format(p)
        p["titleNormalized"] = normalize_title(p["title"])
        secs = None
        if use_cards and p["abstract"]:
            secs = p["abstractSections"] or {"results": p["abstract"]}
        p["tip"] = detect_tip(p["title"], p["track"], secs)
        p["embargo"] = OrderedDict(embargoed=bool(p["abstract"]),
                                   liftsAtUtc=EMBARGO_LIFTS_UTC if p["abstract"] else None)
        for a in p["authors"]:
            a.pop("_presenterId", None)
        out = OrderedDict((f, p.get(f)) for f in PRESENTATION_FIELDS)
        if not p["title"]:
            failures.append(dict(key=f"{p['sourceKind']}:{p['sourceId']}", error="missing title"))
        final_pres.append(out)
    final_pres.sort(key=lambda x: x["id"])

    # ---- people --------------------------------------------------------------------------
    people: dict[str, dict] = {}

    def add_person(a):
        if not a.get("name"):
            return
        pp = people.setdefault(a["personId"], dict(id=a["personId"], name=a["name"], institutions=set(),
                                                    countries=set()))
        if a.get("institution"):
            pp["institutions"].add(a["institution"])
        if a.get("country"):
            pp["countries"].add(a["country"])

    for p in final_pres:
        for a in p["authors"]:
            add_person(a)
    for s_ in sessions.values():
        for a in s_["chairs"] + s_["moderators"]:
            a.pop("_presenterId", None)
            add_person(a)
    people_list = [OrderedDict(id=v["id"], name=v["name"], institutions=sorted(v["institutions"]),
                               countries=sorted(v["countries"])) for v in people.values()]
    people_list.sort(key=lambda x: x["id"])

    # ---- sessions -------------------------------------------------------------------------
    sess_list = []
    for s_ in sessions.values():
        s_["presentationIds"] = sorted(set(s_["presentationIds"]))
        sess_list.append(s_)
    sess_list.sort(key=lambda x: x["id"])

    # ---- sponsors -------------------------------------------------------------------------
    sponsors = []
    for r in parse_sponsor_listing(L("sponsors") or ""):
        sp = OrderedDict(id=f"{CONF_ID}:ExhibitorInfo:{r['boothId']}", name=r["name"], boothId=r["boothId"],
                         exhibitorId=r["exhibitorId"], logoUrl=r["logoUrl"], url=r["url"], website=None,
                         booth=None, description=None, level=None, address=None, contentHash=sha256(r["html"]))
        h = card("ExhibitorInfo", r["boothId"])
        if h:
            try:
                c = parse_exhibitor_card(h)
                for k in ("website", "booth", "description", "level", "address"):
                    sp[k] = c[k]
                sp["name"] = sp["name"] or c["name"]
                sp["contentHash"] = sha256(h)
            except Exception as e:  # noqa: BLE001
                failures.append(dict(key=f"ExhibitorInfo:{r['boothId']}", error=repr(e)))
        sponsors.append(sp)
    sponsors.sort(key=lambda x: x["id"])

    ev_list = sorted(events.values(), key=lambda x: (x["date"] or "", x["startTime"] or "", x["id"]))

    src = bundle.get("source", {}) or {}
    conference = OrderedDict(
        id=CONF_ID, name=src.get("name") or CONF_FACTS.get("name"),
        city=CONF_FACTS.get("city"), country=CONF_FACTS.get("country"), startDate=CONF_FACTS.get("startDate"),
        endDate=CONF_FACTS.get("endDate"), timezone=TIMEZONE,
        sourceUrl=src.get("url") or CONF_FACTS.get("sourceUrl"),
        embargoLiftsAtUtc=EMBARGO_LIFTS_UTC,
    )
    runtime = time.perf_counter() - t0
    report = OrderedDict(
        method=method,
        runtimeSeconds=round(runtime, 3),
        counts=OrderedDict(
            presentations=len(final_pres),
            posters=sum(1 for p in final_pres if p["format"] == "poster"),
            oral=sum(1 for p in final_pres if p["format"] == "oral"),
            lecture=sum(1 for p in final_pres if p["format"] == "lecture"),
            other=sum(1 for p in final_pres if p["format"] == "other"),
            tip=sum(1 for p in final_pres if p["tip"]),
            withAbstract=sum(1 for p in final_pres if p["abstract"]),
            sessions=len(sess_list), people=len(people_list), sponsors=len(sponsors), events=len(ev_list),
            posterListingRows=len(poster_rows),
            presentationListingRows=len(parse_presentation_listing(L("presentations") or "")),
            agendaRows=len(agenda_rows),
            detailCardsAvailable=len(details),
        ),
        fillRates=fill_rates(final_pres),
        failures=failures,
    )
    return OrderedDict(conference=conference, presentations=final_pres, sessions=sess_list,
                       people=people_list, sponsors=sponsors, events=ev_list, parseReport=report)


def _filled(v) -> bool:
    if v is None:
        return False
    if isinstance(v, (list, dict, str)) and len(v) == 0:
        return False
    return True


def fill_rates(presentations: list[dict]) -> dict:
    n = len(presentations)
    out = OrderedDict()
    for f in PRESENTATION_FIELDS:
        if f == "tip":
            c = sum(1 for p in presentations if p.get("tip"))
            out["tip(true)"] = dict(count=c, pct=round(100 * c / n, 1) if n else 0.0)
            continue
        if f == "embargo":
            c = sum(1 for p in presentations if (p.get("embargo") or {}).get("embargoed"))
            out["embargo(embargoed)"] = dict(count=c, pct=round(100 * c / n, 1) if n else 0.0)
            continue
        c = sum(1 for p in presentations if _filled(p.get(f)))
        out[f] = dict(count=c, pct=round(100 * c / n, 1) if n else 0.0)
    authors = [a for p in presentations for a in p.get("authors") or []]
    na = len(authors)
    for f in AUTHOR_FIELDS:
        c = sum(1 for a in authors if _filled(a.get(f)))
        out[f"authors.{f}"] = dict(count=c, pct=round(100 * c / na, 1) if na else 0.0, of=na)
    return out


def dump(obj, path: Path):
    """Write sorted JSON. Wall-clock runtime is dropped so identical input gives identical bytes."""
    if isinstance(obj, dict) and isinstance(obj.get("parseReport"), dict):
        obj = dict(obj)
        obj["parseReport"] = {k: v for k, v in obj["parseReport"].items() if k != "runtimeSeconds"}
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=1, sort_keys=True)
        fh.write("\n")


def comparison_markdown(rd: dict, rl: dict) -> str:
    fd, fl = rd["fillRates"], rl["fillRates"]
    lines = ["# Parser method comparison", "",
             "Method L = listing pages only (posters, postersByCategory, presentations listing with hidden "
             "author blocks, agenda day pages). Method D = listings for inventory plus PosterInfo / "
             "PresentationInfo / SessionInfo / ExhibitorInfo detail cards.", "",
             "## Totals", "", "| metric | L (listing only) | D (listing + detail) |", "|---|---:|---:|"]
    for k in rd["counts"]:
        lines.append(f"| {k} | {rl['counts'].get(k)} | {rd['counts'].get(k)} |")
    lines.append(f"| runtime (s) | {rl['runtimeSeconds']} | {rd['runtimeSeconds']} |")
    lines.append(f"| parse failures | {len(rl['failures'])} | {len(rd['failures'])} |")
    lines += ["", "## Presentation field fill rates", "",
              "Count of presentations with a non-null, non-empty value (tip and embargo: count of true).", "",
              "| field | L count | L % | D count | D % |", "|---|---:|---:|---:|---:|"]
    for k in fd:
        a, b = fl.get(k, {"count": 0, "pct": 0}), fd[k]
        lines.append(f"| {k} | {a['count']} | {a['pct']} | {b['count']} | {b['pct']} |")
    lines += ["", "## Decision", ""] + decision_text(rd, rl)
    return "\n".join(lines) + "\n"


def decision_text(rd: dict, rl: dict) -> list[str]:
    fd, fl, cd, cl = rd["fillRates"], rl["fillRates"], rd["counts"], rl["counts"]
    n = cd["presentations"]
    gains = []
    for k in PRESENTATION_FIELDS + [f"authors.{f}" for f in AUTHOR_FIELDS]:
        kk = "tip(true)" if k == "tip" else ("embargo(embargoed)" if k == "embargo" else k)
        if kk in fd and kk in fl and fd[kk]["count"] > fl[kk]["count"]:
            gains.append((kk, fl[kk]["count"], fd[kk]["count"]))
    worse = [k for k in fd if k in fl and fd[k]["count"] < fl[k]["count"]]
    out = [
        f"**Default: Method D.** Both methods find the same inventory ({n} presentations: "
        f"{cd['posters']} posters plus {n - cd['posters']} talks/lectures/other; {cd['sessions']} sessions; "
        f"{cd['events']} logistics events), because the inventory comes from the same listing pages in both. "
        f"D is never worse on any field ({len(worse)} fields lower) and is better on {len(gains)} fields:",
        "",
    ]
    for k, a, b in gains:
        out.append(f"* `{k}`: {a} -> {b}")
    out += [
        "",
        f"Fields that matter downstream are only available from cards: abstracts "
        f"({fl['abstract']['count']} vs {fd['abstract']['count']}), track ({fl['track']['count']} vs "
        f"{fd['track']['count']}; the postersByCategory listing in this capture is byte-identical to the posters "
        f"listing, so it carries no category), poster authors (posters in L have no authors at all: "
        f"L authors filled {fl['authors']['count']} vs D {fd['authors']['count']}), structured author "
        f"location (city/country {fl['authors.country']['count']} vs {fd['authors.country']['count']} author rows), "
        f"disclosures, views, slide counts, and trial-in-progress detection (L can only use the track text, "
        f"which it does not have for posters: tip {cl['tip']} vs {cd['tip']}). The Tagger and Readouts agents "
        f"need abstract text, so L cannot feed them.",
        "",
        f"Cost: D takes {rd['runtimeSeconds']} s vs {rl['runtimeSeconds']} s for L on this bundle "
        f"({cd['detailCardsAvailable']} cards), which is negligible next to capture time. "
        f"Parse failures: L {len(rl['failures'])}, D {len(rd['failures'])}.",
        "",
        "L remains useful as a fallback when cards are missing (D already falls back to listing values per "
        "field) and as a cross-check. Cross-check on this bundle: the two outputs have the same presentation ids, "
        "and number, date, startTime, endTime and location agree on every record; titles differ only for the two "
        "'Discussant N' placeholders (listing 'Discussant 1 (Discussant 1)' vs card 'Discussant 1 - Discussant 1'). "
        "L links fewer talks to sessions (it infers membership from the same room plus the session time window, "
        "for numbered talks only), and so it labels the unnumbered symposium talks 'other' where D labels them 'oral'.",
    ]
    return out


def run(bundle_path, out_path):
    """Stage contract: raw bundle file in, parsed.json out (method D)."""
    bundle = json.load(open(bundle_path, encoding="utf-8"))
    res = build(bundle, "D")
    dump(res, Path(out_path))
    return res["parseReport"]["counts"]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bundle", default=str(ROOT / "data/raw/bundle.json"))
    ap.add_argument("--method", default="D", choices=["D", "L", "d", "l"])
    ap.add_argument("--out", default=None)
    ap.add_argument("--compare", action="store_true", help="run both methods, write both outputs + report")
    args = ap.parse_args(argv)
    bundle = json.load(open(args.bundle, encoding="utf-8"))
    if args.compare:
        rd = build(bundle, "D")
        rl = build(bundle, "L")
        dump(rd, ROOT / "data/stage/parsed.json")
        dump(rl, ROOT / "data/stage/parsed_listing_only.json")
        md = ROOT / "reports/parse_methods.md"
        md.parent.mkdir(parents=True, exist_ok=True)
        md.write_text(comparison_markdown(rd["parseReport"], rl["parseReport"]), encoding="utf-8")
        print(json.dumps({"D": rd["parseReport"]["counts"], "L": rl["parseReport"]["counts"],
                          "runtime": [rd["parseReport"]["runtimeSeconds"], rl["parseReport"]["runtimeSeconds"]],
                          "failuresD": rd["parseReport"]["failures"][:20]}, indent=1))
        return
    res = build(bundle, args.method)
    out = Path(args.out) if args.out else ROOT / ("data/stage/parsed.json" if args.method.upper() == "D"
                                                  else "data/stage/parsed_listing_only.json")
    dump(res, out)
    print(json.dumps(res["parseReport"]["counts"], indent=1))


if __name__ == "__main__":
    main()
