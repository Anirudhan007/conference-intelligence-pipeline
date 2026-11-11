"""Rebuild data/raw/bundle.json from compressed browser chunks ('CHUNK<i>|<base64>').

Usage:
    python capture/decode_chunks.py --sha256 <hash from prepareCompressed> chunk_files... -o data/raw/bundle.json

Chunk files may be plain text files holding one chunk string, or harness tool result files.
The SHA-256 of the decompressed JSON must match the hash the browser reported.
"""
import argparse
import base64
import gzip
import hashlib
import json
import re
import sys
from pathlib import Path


def read_chunks(paths):
    chunks = {}
    for p in paths:
        txt = open(p).read()
        candidates = [txt]
        try:
            raw = json.loads(txt)
            if isinstance(raw, list):
                candidates = [x.get("text", "") for x in raw if isinstance(x, dict)]
            elif isinstance(raw, str):
                candidates = [raw]
        except json.JSONDecodeError:
            pass
        for t in candidates:
            t = t.split("\n\n(captured at origin")[0].strip()
            if t.startswith('"'):
                t = json.loads(t)
            m = re.match(r"^CHUNK(\d+)\|", t)
            if m:
                chunks[int(m.group(1))] = t.split("|", 1)[1]
    return chunks


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+")
    ap.add_argument("--sha256", required=True)
    ap.add_argument("-o", "--out", default="data/raw/bundle.json")
    a = ap.parse_args()
    chunks = read_chunks(a.files)
    n = max(chunks) + 1
    missing = [i for i in range(n) if i not in chunks]
    if missing:
        sys.exit(f"missing chunks {missing}")
    data = gzip.decompress(base64.b64decode("".join(chunks[i] for i in range(n))))
    sha = hashlib.sha256(data).hexdigest()
    if sha != a.sha256:
        sys.exit(f"sha256 mismatch: got {sha}")
    bundle = json.loads(data)
    for rec in list(bundle["listings"].values()) + list(bundle["details"].values()):
        rec["sha256"] = hashlib.sha256(rec["html"].encode()).hexdigest()
    bundle["source"]["bundleSha256"] = sha
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as fh:
        json.dump(bundle, fh)
    print(f"wrote {a.out}: {len(bundle['listings'])} listings, {len(bundle['details'])} details, sha256 ok")


if __name__ == "__main__":
    main()
