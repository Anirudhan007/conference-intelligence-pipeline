"""Tests for pipeline/parse.py against synthetic EventScribe samples in tests/fixtures_synthetic/."""
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pipeline"))
import parse  # noqa: E402

FX = json.loads((ROOT / "tests" / "fixtures_synthetic" / "eventscribe_synthetic.json").read_text(encoding="utf-8"))


def _bundle():
    details = {}
    for c in FX["poster_cards"]:
        details["PosterInfo:" + re.search(r"PosterID=(\d+)", c["url"]).group(1)] = c
    for c in FX["pres_cards"]:
        details["PresentationInfo:" + re.search(r"PresentationID=(\d+)", c["url"]).group(1)] = c
    c = FX["session_card"]
    details["SessionInfo:" + re.search(r"PresentationID=(\d+)", c["url"]).group(1)] = c
    return {
        "source": {"url": "https://ctos2026.eventscribe.net/index.asp?launcher=1"},
        "listings": {
            "posters": {"html": FX["poster_listing"]},
            "presentations": {"html": FX["title_listing"]},
            "day_2026_11_20": {"html": FX["agenda_day"]},
            "sponsors": {"html": FX["sponsors"]},
        },
        "details": details,
    }


@pytest.fixture(scope="module")
def parsed_d():
    return parse.build(_bundle(), "D")


@pytest.fixture(scope="module")
def parsed_l():
    return parse.build(_bundle(), "L")


def by_source(parsed, sid):
    return next(p for p in parsed["presentations"] if p["sourceId"] == sid)


# ---------------------------------------------------------------- unit helpers

@pytest.mark.parametrize("text,expected", [
    ("6:30 AM - 5:30 PM KST", ("06:30", "17:30")),
    ("12:00 PM - 1:15 PM KST", ("12:00", "13:15")),
    ("12:15 AM - 1:00 AM", ("00:15", "01:00")),
    ("9:00 AM - 10:30 AM <small>KST</small>", ("09:00", "10:30")),
    ("no time", (None, None)),
])
def test_time_span(text, expected):
    assert parse.parse_time_span(text) == expected


def test_date():
    assert parse.parse_date("Saturday, November 21, 2026") == "2026-11-21"
    assert parse.parse_date(None) is None


@pytest.mark.parametrize("raw,name,degrees,pronouns", [
    ("Alex R. Morgan, MD (she/her/hers)", "Alex R. Morgan", ["MD"], "she/her/hers"),
    ("Matthew R. Cole", "Matthew R. Cole", [], None),
    ("Lorenzo Bellini (he/him/his)", "Lorenzo Bellini", [], "he/him/his"),
    ("Valerie K. Tan, MB BChir, PhD, MRCP, FAMS", "Valerie K. Tan", ["MB BChir", "PhD", "MRCP", "FAMS"], None),
    ("Hendrik V. Maas, MD PhD (he/him/his)", "Hendrik V. Maas", ["MD", "PhD"], "he/him/his"),
    ("Luca Conti, N/A, MSc", "Luca Conti", ["MSc"], None),
    ("Camille Moreau, n/a", "Camille Moreau", [], None),
])
def test_split_name(raw, name, degrees, pronouns):
    assert parse.split_name(raw) == (name, degrees, pronouns)


def test_prof_lines():
    assert parse.parse_prof_lines(["Professor", "Northbridge Cancer Institute", "Boston, Massachusetts, United States"]) == (
        "Professor", "Northbridge Cancer Institute", "Boston", "Massachusetts", "United States")
    assert parse.parse_prof_lines(["Northbridge Cancer Institute", "Boston, Massachusetts, United States"]) == (
        None, "Northbridge Cancer Institute", "Boston", "Massachusetts", "United States")
    assert parse.parse_prof_lines(["Civic Sarcoma Support Trust", "London, United Kingdom"]) == (
        None, "Civic Sarcoma Support Trust", "London", None, "United Kingdom")


def test_titles():
    assert parse.split_paper_title("Paper 86 - A BAYESIAN FRAMEWORK") == ("Paper 86", "A BAYESIAN FRAMEWORK")
    assert parse.split_paper_title("TITLE HERE (Paper 82)") == ("Paper 82", "TITLE HERE")
    assert parse.split_paper_title("Psper 33 - SPECTRUM") == ("Paper 33", "SPECTRUM")
    assert parse.split_paper_title("Herman Suit Lecture: X") == (None, "Herman Suit Lecture: X")
    assert parse.split_poster_title("(P 01) EARLY DATA") == ("P 01", "EARLY DATA")


def test_tip_detection():
    lba = "Trials In Progress & Late Breaking Abstracts"
    assert parse.detect_tip("X", lba, {"results": "N/A"})
    assert not parse.detect_tip("X", lba, {"results": "ORR was 45% (n=20)."})
    assert parse.detect_tip("X", lba, None)  # listing only: track decides
    assert parse.detect_tip("A TRIAL IN PROGRESS: X", "Basic Science", None)
    assert not parse.detect_tip("X", "Basic Science", None)


def test_person_id_stable():
    a = parse.person_id("Alex R. Morgan", "Northbridge Cancer Institute")
    assert a == parse.person_id("alex r morgan", "Northbridge  Cancer Institute")
    assert a != parse.person_id("Alex R. Morgan", "Other Place")


# ---------------------------------------------------------------- listing parsers

def test_poster_listing():
    rows = parse.parse_poster_listing(FX["poster_listing"])
    assert rows[0]["posterId"] == "900101"
    assert rows[0]["number"] == "P 01"
    assert rows[0]["title"] == "SYNTHETIC COHORT STUDY OF LOCAL ABLATIVE RADIOTHERAPY IN SOFT TISSUE SARCOMA"


def test_presentation_listing_hidden_authors():
    rows = parse.parse_presentation_listing(FX["title_listing"])
    r = rows[0]
    assert r["presId"] == "910082" and r["number"] == "Paper 82"
    assert r["title"] == "DECADE REVIEW OF PATIENT NAVIGATION IN SARCOMA CARE"
    a0, a1 = r["authors"][:2]
    assert a0["name"] == "Jordan Taylor" and a0["role"] == "author"
    assert a0["position"] == "Clinical Navigation Lead" and a0["institution"] == "Civic Sarcoma Support Trust"
    assert a1["name"] == "Riley M. Chen" and a1["role"] == "coauthor" and a1["pronouns"] == "she/her/hers"
    roles = [a["role"] for a in rows[1]["authors"]]
    assert "presenter" in roles


def test_agenda_day():
    rows = parse.parse_agenda_day(FX["agenda_day"])
    reg = rows[0]
    assert reg["rawTitle"] == "Registration" and reg["date"] == "2026-11-20"
    assert (reg["startTime"], reg["endTime"]) == ("06:30", "18:00")
    assert reg["location"] == "Hall D Foyer, 3F"
    sess = next(r for r in rows if r["kind"] == "SessionInfo")
    assert sess["id"] == "920007"
    assert [p["role"] for p in sess["people"]] == ["chair", "moderator", "moderator"]
    # bucket header rows are not duplicated
    assert len({(r["kind"], r["id"]) for r in rows}) == len(rows)


# ---------------------------------------------------------------- method D end-to-end

def test_poster_card_fields(parsed_d):
    p = by_source(parsed_d, "900101")
    assert p["id"] == "ctos-2026:PosterInfo:900101"
    assert p["number"] == "P 01" and p["format"] == "poster"
    assert p["track"] == "Advancing Local Therapy Technologies: Radiotherapy & Other Ablative Techniques"
    first = p["authors"][0]
    assert first == {**first, "name": "Alex R. Morgan", "degrees": ["MD"], "pronouns": None, "role": "author",
                     "position": "Professor", "institution": "Northbridge Cancer Institute", "city": "Boston",
                     "region": "Massachusetts", "country": "United States", "order": 1}
    roles = [a["role"] for a in p["authors"]]
    assert roles.count("presenter") == 1 and roles[0] == "author"
    presenter = next(a for a in p["authors"] if a["role"] == "presenter")
    assert presenter["name"] == "Evan T. Brooks" and presenter["pronouns"] == "he/him/his"
    assert [a["order"] for a in p["authors"]] == list(range(1, len(p["authors"]) + 1))
    # no position line -> position null, institution + location kept
    patel = next(a for a in p["authors"] if a["name"] == "Dana K. Patel")
    assert patel["position"] is None and patel["institution"] == "Northbridge Cancer Institute"


def test_abstract(parsed_d):
    p = by_source(parsed_d, "900101")
    lines = p["abstract"].split("\n")
    assert lines[0].startswith("Objective: Stereotactic Body Radiotherapy")
    assert lines[1].startswith("Methods: Eligible patients")
    assert lines[2].startswith("Results: ")
    assert "<" not in p["abstract"]
    assert set(p["abstractSections"]) >= {"objective", "methods", "results"}
    assert p["abstractSections"]["objective"].startswith("Stereotactic Body Radiotherapy")
    assert p["embargo"] == {"embargoed": True, "liftsAtUtc": "2026-11-18T14:00:00Z"}
    full = by_source(parsed_d, "900102")
    assert full["abstract"].split("\n")[-1].startswith("Conclusion: Our results uncover")
    assert "Disclosure" not in full["abstract"]


def test_tip_poster_and_disclosures(parsed_d):
    p = by_source(parsed_d, "900654")
    assert p["number"] == "P 654" and p["tip"] is True
    assert p["abstractSections"]["results"] == "N/A"
    assert p["disclosures"][0] == {"name": "Mei Lin", "text": "No relevant disclosure to display"}
    assert not by_source(parsed_d, "900101")["tip"]


def test_presentation_card(parsed_d):
    p = by_source(parsed_d, "910082")
    assert p["number"] == "Paper 82" and p["format"] == "oral"
    assert p["title"] == "DECADE REVIEW OF PATIENT NAVIGATION IN SARCOMA CARE"
    assert p["sessionTitle"] == "Session 16: Patient-Centered Care & Survivorship: Living with and Beyond Sarcoma"
    assert (p["date"], p["startTime"], p["endTime"]) == ("2026-11-21", "14:15", "15:15")
    assert p["location"] == "Auditorium, 3F" and p["timezone"] == "Asia/Seoul"
    a = p["authors"][0]
    assert a["name"] == "Jordan Taylor"
    assert a["degrees"] == ["RGN", "Bsc ONC Nursing", "Post Grad Dip Nursing"]
    assert (a["city"], a["region"], a["country"]) == ("London", "England", "United Kingdom")
    assert p["disclosures"] == [{"name": "Jordan Taylor", "text": "No financial relationships to disclose"}]
    assert p["abstract"] is None and p["embargo"] == {"embargoed": False, "liftsAtUtc": None}
    assert len(p["contentHash"]) == 64


def test_lecture(parsed_d):
    p = by_source(parsed_d, "910963")
    assert p["format"] == "lecture" and p["number"] is None
    assert p["authors"][0]["role"] == "presenter" and p["authors"][0]["name"] == "Claire H. Morales"
    assert by_source(parsed_d, "910962")["format"] == "lecture"


def test_session(parsed_d):
    s = next(x for x in parsed_d["sessions"] if x["id"] == "ctos-2026:SessionInfo:920007")
    assert s["title"] == "Session 7: Sarcoma of the Year Rhabdomyosarcoma"
    assert (s["date"], s["startTime"], s["endTime"], s["location"]) == ("2026-11-20", "09:00", "10:30",
                                                                      "Auditorium, 3F")
    assert [c["name"] for c in s["chairs"]] == ["Priya N. Shah"]
    assert [m["name"] for m in s["moderators"]] == ["Marcus D. Reed", "Mei Lin"]
    assert "ctos-2026:PresentationInfo:910030" in s["presentationIds"]
    child = by_source(parsed_d, "910030")
    assert child["sessionId"] == s["id"] and child["slidesAvailable"] == 12
    assert child["number"] == "Paper 30"
    assert by_source(parsed_d, "910033")["number"] == "Paper 33"  # agenda typo "Psper 33"
    assert by_source(parsed_d, "910033")["slidesAvailable"] is None


def test_logistics_go_to_events(parsed_d):
    ids = {p["id"] for p in parsed_d["presentations"]}
    ev = {e["title"]: e for e in parsed_d["events"]}
    assert "Registration" in ev and "Coffee Break and Posters" in ev
    assert ev["Registration"]["id"] not in ids
    assert ev["Registration"]["format"] == "other"
    assert (ev["Registration"]["startTime"], ev["Registration"]["endTime"]) == ("06:30", "18:00")


def test_people_dedup(parsed_d):
    ids = [p["id"] for p in parsed_d["people"]]
    assert len(ids) == len(set(ids))
    referenced = {a["personId"] for p in parsed_d["presentations"] for a in p["authors"]}
    assert referenced <= set(ids)
    # Mei Lin (same institution string) appears as session moderator and poster author
    mei_entries = [p for p in parsed_d["people"] if p["name"] == "Mei Lin"]
    assert len(mei_entries) == 1
    names = [p["name"] for p in parsed_d["people"]]
    assert "Alex R. Morgan" in names


def test_contract_shape(parsed_d):
    assert set(parsed_d) == {"conference", "presentations", "sessions", "people", "sponsors", "events",
                             "parseReport"}
    for p in parsed_d["presentations"]:
        assert list(p) == parse.PRESENTATION_FIELDS
        assert p["format"] in {"poster", "oral", "lecture", "other"}
        assert p["id"] == f"ctos-2026:{p['sourceKind']}:{p['sourceId']}"
        assert isinstance(p["tip"], bool) and isinstance(p["authors"], list)
        assert p["embargo"]["embargoed"] == bool(p["abstract"])
        for a in p["authors"]:
            assert list(a) == parse.AUTHOR_FIELDS
            assert a["role"] in {"author", "coauthor", "presenter", "chair", "moderator", "speaker"}
    assert parsed_d["parseReport"]["failures"] == []
    assert [s["name"] for s in parsed_d["sponsors"]] == [
        "Apex BioLabs Inc.",
        "Helixia Therapeutics",
        "Vanguard Oncology",
    ]


def test_deterministic():
    a = json.dumps(parse.build(_bundle(), "D")["presentations"], sort_keys=True)
    b = json.dumps(parse.build(_bundle(), "D")["presentations"], sort_keys=True)
    assert a == b


# ---------------------------------------------------------------- method L

def test_listing_only(parsed_l, parsed_d):
    p = by_source(parsed_l, "900101")
    assert p["number"] == "P 01" and p["abstract"] is None and p["authors"] == []
    t = by_source(parsed_l, "910030")
    assert t["number"] == "Paper 30" and t["date"] == "2026-11-20" and t["startTime"] == "09:00"
    assert t["sessionId"] == "ctos-2026:SessionInfo:920007"  # inferred by time window + room
    assert t["authors"][0]["name"] == "Hendrik V. Maas"
    h = by_source(parsed_l, "910082")
    assert h["authors"][0]["position"] == "Clinical Navigation Lead"
    # D fills at least as much as L for every field
    fd, fl = parsed_d["parseReport"]["fillRates"], parsed_l["parseReport"]["fillRates"]
    for k in ("track", "abstract", "disclosures", "authors"):
        assert fd[k]["count"] >= fl[k]["count"]
    assert fl["abstract"]["count"] == 0


# ---------------------------------------------------------------- shapes seen in EventScribe bundles

def test_views_from_gutter():
    html = ('<div class="card updated" id="poster-info-1"><div class="popup_gutter"> <div class="popup_gutter_left">'
            ' <span class="hidden-xs hidden-sm">128 Views</span></div> <div class="popup_gutter_right"> </div> </div>'
            '<hr><div class="popup_content"><h1>(P 01) X</h1></div></div>')
    assert parse.parse_poster_card(html)["views"] == 128
    assert parse.parse_poster_card(FX["poster_cards"][0]["html"])["views"] is None  # fixture lacks gutter


def test_poster_discussion_numbers():
    assert parse.split_paper_title("Poster 2 - SYSTEMIC STING AGONIST") == ("Poster 2", "SYSTEMIC STING AGONIST")
    assert parse.split_paper_title("Discussant 1 (Discussant 1)") == (None, "Discussant 1 (Discussant 1)")


def test_listing_session_rows_become_sessions():
    li = ('<li class="list-group-item loadbyurl" data-presid="920001" '
          'data-url="ajaxcalls/SessionInfo.asp?PresentationID=920001"><div class="prestitle"><a><span>'
          'Session 1: Sarcoma Across Borders</span></a></div><i class="hidden"> Elena K. Bauer, MD, MSc '
          '(she/her/hers) – Alpine Medical University ### 8001###Chair###Specialist###Alpine Medical '
          'University###<p>Chair: x</p> ### she/her/hers @@@ Min-Jae Park, MD – Metro University Hospital ### '
          '8002###Moderator######Metro University Hospital###<p>Moderator: y</p> ### </i></li>')
    out = parse.build({"listings": {"presentations": {"html": li}}, "details": {}}, "L")
    assert out["presentations"] == []
    s = out["sessions"][0]
    assert s["id"] == "ctos-2026:SessionInfo:920001" and s["title"] == "Session 1: Sarcoma Across Borders"
    assert [c["name"] for c in s["chairs"]] == ["Elena K. Bauer"]
    assert [m["name"] for m in s["moderators"]] == ["Min-Jae Park"]


def test_name_suffix_and_format_classifier():
    assert parse.split_name("John Smith, Jr., MD") == ("John Smith Jr.", ["MD"], None)
    talk = {"sourceKind": "PresentationInfo", "number": None, "title": "EPIGENOMIC LANDSCAPES OF X",
            "sessionId": "s", "authors": [{"role": "author"}]}
    assert parse.classify_format(talk) == "oral"
    assert parse.classify_format({**talk, "title": "Symposium Opening Remarks"}) == "other"
    assert parse.classify_format({**talk, "sessionId": None}) == "other"
    assert parse.is_logistics("PRO-Sarcoma Meeting") and parse.is_logistics("Special Entertainment")
    assert not parse.is_logistics("Herman Suit Lecture: X")


def test_exhibitor_card():
    html = ('<div class="card"><div id="about"><h1>Apex BioLabs Inc.</h1><div><img src="x/Badges/platinum.png">'
            '</div><pre>Apex is a synthetic company.</pre></div><div id="contactinfo"><p class="CompanyAddress1">'
            '<b>Apex</b><br>100 Innovation Way<br>Cambridge, MA</p><a href="/tracking">https://example.org/apex</a>'
            '</div></div>')
    c = parse.parse_exhibitor_card(html)
    assert c["name"] == "Apex BioLabs Inc." and c["level"] == "platinum"
    assert c["website"] == "https://example.org/apex" and c["description"] == "Apex is a synthetic company."
    assert c["address"] == "100 Innovation Way; Cambridge, MA"
