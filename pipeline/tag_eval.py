"""Gold-set selection, labelling dump and scoring for the tagger (Agent Tagger).

  python -m pipeline.tag_eval select   # writes data/gold/gold_selection.json (80 ids, dev/test split)
  python -m pipeline.tag_eval dump --split dev|test   # prints title/abstract for manual labelling
  python -m pipeline.tag_eval score [--split test] [--json out.json]

The selection is independent of both taggers: the stratum "mentions drugs or biomarkers" is defined by a
neutral regex (INN-like suffixes or biomarker status words), not by Method E or K output.
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sys
from collections import defaultdict

from pipeline.tag import get_kb, norm_key

SEED = 20261118
PARSED = "data/stage/parsed.json"
GOLD = "data/gold/tags_gold.json"
SEL = "data/gold/gold_selection.json"

NEUTRAL_DRUG = re.compile(r"\b\w{3,}(?:mab|tinib|nib|ciclib|parib|platin|taxel|rubicin|famide|tecan|stat|lisib|leucel|cel|cept|"
                          r"citabine|bulin|ectedin|tug|bart|madlin|zomib|limus)\b|\binhibitors?\b|\bchemotherapy\b", re.I)
NEUTRAL_BIOM = re.compile(r"\b(?:mutations?|mutant|mutated|fusions?|amplifi\w+|rearrange\w*|wild[- ]type|deficient|"
                          r"overexpress\w*|IHC|immunohistochem\w*)\b", re.I)


def load(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def cmd_select(a):
    parsed = load(PARSED)
    pres = sorted(parsed["presentations"], key=lambda p: p["id"])
    pool = [p["id"] for p in pres if p.get("abstract") and (NEUTRAL_DRUG.search(p["abstract"]) or NEUTRAL_BIOM.search(p["abstract"]))]
    rng = random.Random(SEED)
    strat_a = sorted(rng.sample(pool, min(40, len(pool))))
    rest = [p["id"] for p in pres if p["id"] not in set(strat_a)]
    strat_b = sorted(rng.sample(rest, 40))
    dev, test = [], []
    for stratum in (strat_a, strat_b):
        sh = list(stratum)
        rng.shuffle(sh)
        dev += sorted(sh[: len(sh) // 2])
        test += sorted(sh[len(sh) // 2:])
    out = {"seed": SEED, "poolSize": len(pool), "nPresentations": len(pres),
           "strata": {"drugOrBiomarker": strat_a, "random": strat_b},
           "split": {"dev": sorted(dev), "test": sorted(test)}}
    with open(SEL, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=1, sort_keys=True)
    print(f"pool {len(pool)}; dev {len(dev)} test {len(test)}")


def cmd_dump(a):
    parsed = load(PARSED)
    sel = load(SEL)
    byid = {p["id"]: p for p in parsed["presentations"]}
    ids = sel["split"][a.split]
    if a.start is not None:
        ids = ids[a.start: a.start + a.n]
    for pid in ids:
        p = byid[pid]
        print("=" * 100)
        print(pid, "| track:", p.get("track"), "| session:", p.get("sessionTitle"))
        print("TITLE:", p.get("title"))
        print("ABSTRACT:", p.get("abstract"))


# ------------------------------------------------------------------ scoring
def k_drug_id(eid: str) -> str:
    return eid


def disease_min(kb, ids):
    ids = {i for i in ids if i in kb.disease}
    anc = set()
    for i in ids:
        anc.update(kb.ancestors(i))
    return ids - anc


def drug_norm(eid: str) -> str:
    """Unknown names that the KB now knows (e.g. added from the review list) map to the KB id."""
    if eid.startswith("unknown:"):
        key = norm_key(eid.split(":", 1)[1])
        hits = get_kb().drug_ci.map.get(key)
        if hits:
            return hits[0][0]
        return "unknown:" + key
    return eid


def pred_sets(kb, tags):
    s = {"disease": set(), "drug": set(), "drug_target": set(), "modality": set(), "polarity": set()}
    for t in tags:
        k = t["kind"]
        if k == "disease":
            s["disease"].add(t["entityId"])
        elif k == "drug":
            s["drug"].add(drug_norm(t["entityId"]))
        elif k == "modality":
            s["modality"].add(t["entityId"])
        elif k == "target":
            if t["role"] == "drug_target":
                s["drug_target"].add(t["entityId"])
            elif t.get("polarity") not in (None, "unknown"):
                s["polarity"].add((kb.target_group(t["entityId"]), t["polarity"]))
    s["disease"] = disease_min(kb, s["disease"])
    return s


def gold_sets(kb, g):
    return {"disease": disease_min(kb, set(g.get("diseases", []))),
            "drug": {drug_norm(d["id"]) for d in g.get("drugs", [])},
            "drug_target": set(g.get("drugTargets", [])),
            "modality": set(g.get("modalities", [])),
            "polarity": {(kb.target_group(x["gene"]), x["polarity"]) for x in g.get("polarity", [])}}


KINDS = ["disease", "drug", "drug_target", "modality", "polarity"]


def score(kb, gold, tags, ids):
    tp, fp, fn = defaultdict(int), defaultdict(int), defaultdict(int)
    errors = defaultdict(list)
    for pid in ids:
        g = gold_sets(kb, gold["items"][pid])
        p = pred_sets(kb, tags.get(pid, []))
        for k in KINDS:
            tp[k] += len(g[k] & p[k])
            fp[k] += len(p[k] - g[k])
            fn[k] += len(g[k] - p[k])
            for x in sorted(map(str, p[k] - g[k])):
                errors[k].append((pid, "FP", x))
            for x in sorted(map(str, g[k] - p[k])):
                errors[k].append((pid, "FN", x))
    res = {}
    for k in KINDS:
        P = tp[k] / (tp[k] + fp[k]) if tp[k] + fp[k] else None
        R = tp[k] / (tp[k] + fn[k]) if tp[k] + fn[k] else None
        F = 2 * P * R / (P + R) if P and R else (0.0 if P is not None and R is not None else None)
        res[k] = {"tp": tp[k], "fp": fp[k], "fn": fn[k], "precision": P, "recall": R, "f1": F}
    return res, errors


def role_accuracy(gold, tags, ids):
    agree = total = 0
    for pid in ids:
        groles = {drug_norm(d["id"]): d.get("role") for d in gold["items"][pid].get("drugs", [])}
        for t in tags.get(pid, []):
            if t["kind"] == "drug" and drug_norm(t["entityId"]) in groles:
                total += 1
                agree += int(t.get("role") == groles[drug_norm(t["entityId"])])
    return (agree / total if total else None), total


def coverage(parsed, tags):
    pres = parsed["presentations"]
    n = len(pres)
    out = {}
    for kind in ("disease", "drug", "modality"):
        c = sum(1 for p in pres if any(t["kind"] == kind for t in tags.get(p["id"], [])))
        out[kind] = {"n": c, "pct": round(100 * c / n, 1) if n else None}
    wa = [p for p in pres if p.get("abstract")]
    out["withAbstract"] = len(wa)
    out["n"] = n
    return out


def cmd_score(a):
    kb = get_kb()
    gold = load(GOLD)
    tE = load("data/stage/tags.json")
    tK = load("data/stage/tags_keyword.json")
    ids = gold["split"][a.split] if a.split != "all" else gold["split"]["dev"] + gold["split"]["test"]
    out = {}
    for name, tags in (("K", tK), ("E", tE)):
        res, errs = score(kb, gold, tags, ids)
        acc, n = role_accuracy(gold, tags, ids)
        out[name] = {"scores": res, "drugRoleAccuracy": acc, "drugRoleN": n, "errors": {k: v for k, v in errs.items()}}
        print(f"== Method {name} on {a.split} ({len(ids)})")
        for k in KINDS:
            r = res[k]
            f = lambda x: "  -  " if x is None else f"{x:.3f}"
            print(f"  {k:12s} P {f(r['precision'])} R {f(r['recall'])} F1 {f(r['f1'])}  (tp {r['tp']} fp {r['fp']} fn {r['fn']})")
        print(f"  drug role accuracy {acc} over {n}")
        if a.errors and name == "E":
            for k in KINDS:
                for e in errs.get(k, [])[: a.errors]:
                    print("   ", k, *e)
    if a.json:
        with open(a.json, "w", encoding="utf-8") as fh:
            json.dump(out, fh, indent=1, sort_keys=True, default=str)


def main(argv=None):
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("select")
    s.set_defaults(fn=cmd_select)
    d = sub.add_parser("dump")
    d.add_argument("--split", default="dev")
    d.add_argument("--start", type=int, default=None)
    d.add_argument("--n", type=int, default=10)
    d.set_defaults(fn=cmd_dump)
    c = sub.add_parser("score")
    c.add_argument("--split", default="test", choices=["dev", "test", "all"])
    c.add_argument("--errors", type=int, default=0)
    c.add_argument("--json", default=None)
    c.set_defaults(fn=cmd_score)
    a = ap.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
