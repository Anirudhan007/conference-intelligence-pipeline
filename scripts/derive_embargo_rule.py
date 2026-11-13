"""Write a reviewable public-release rule into a conference config from captured evidence.

* crossref bundles: the earliest publication date among the meeting's records, quoted verbatim from the
  captured Crossref JSON (sourceUrl = that record's API URL);
* past EventScribe meetings with no captured embargo statement: a rule marked ``quoteType: derived`` at the
  meeting's end date, stating that abstracts were presented publicly (review before external use).

Usage: python scripts/derive_embargo_rule.py configs/<id>.json [data/raw/<id>.json]
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path


def crossref_rule(bundle: dict) -> dict:
    best = None
    for rec in (bundle.get("details") or {}).values():
        body = rec["body"]
        item = json.loads(body)
        for field in ("published-online", "published-print", "published"):
            parts = ((item.get(field) or {}).get("date-parts") or [[None]])[0]
            if not parts or not parts[0]:
                continue
            y, m, d = (list(parts) + [1, 1])[:3]
            date = f"{int(y):04d}-{int(m or 1):02d}-{int(d or 1):02d}"
            quote_m = re.search(r'"' + re.escape(field) + r'": \{"date-parts": \[\[[^\]]*\]\]\}', body)
            if quote_m and (best is None or date < best[0]):
                best = (date, quote_m.group(0), rec["url"])
            break
    if best is None:
        raise SystemExit("no publication date found in bundle")
    return {"class": "public", "releaseUtc": f"{best[0]}T00:00:00Z", "sourceQuote": best[1], "sourceUrl": best[2],
            "quoteType": "verbatim", "note": "Earliest journal publication date among the meeting's abstract records."}


def main() -> None:
    cfg_path = Path(sys.argv[1])
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    conf = cfg["conference"]
    if conf["platform"] == "crossref":
        bundle = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
        rule = crossref_rule(bundle)
    else:
        rule = {"class": "public", "releaseUtc": f"{conf['endDate']}T23:59:00Z",
                "sourceQuote": f"Meeting held {conf['startDate']} to {conf['endDate']} in {conf['city']}; abstracts "
                               "were presented publicly at the meeting.",
                "sourceUrl": conf.get("officialUrl") or conf["sourceUrl"], "quoteType": "derived",
                "note": "No embargo statement was captured; release taken as the meeting end. Review before release."}
    cfg.setdefault("embargo", {})["rules"] = [rule]
    cfg_path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(rule, indent=2))


if __name__ == "__main__":
    main()
