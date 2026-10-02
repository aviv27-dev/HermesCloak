#!/usr/bin/env python3
"""Scan recorded model-bound requests for known identifying values (counts only, never values).

  python install/leak_scan.py --manifest tests/fixtures/verdicts/manifest.json --log out.jsonl

Prints, per value type: how many of the manifest's values were mentioned in the conversation's
model traffic as a token vs. in clear. A "masked" value seen in clear is a LEAK (exit 1);
"gap" values are reported as measurements. Values are matched in the JSON-decoded request text,
so escaping (\\u05d0, \\") cannot hide a leak.
"""
import argparse
import collections
import json
import sys


def _strings(obj, out):
    if isinstance(obj, str):
        out.append(obj)
    elif isinstance(obj, list):
        for x in obj:
            _strings(x, out)
    elif isinstance(obj, dict):
        for v in obj.values():
            _strings(v, out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--log", required=True)
    ap.add_argument("--hermes-home", default=None,
                    help="read the sandbox vault to tell 'sent as a token' from 'never sent'")
    a = ap.parse_args()
    manifest = json.load(open(a.manifest, encoding="utf-8"))
    texts, n = [], 0
    try:
        for line in open(a.log, encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            n += 1
            try:
                _strings(json.loads(line), texts)
            except ValueError:
                texts.append(line)
    except OSError:
        print("no recorded requests (log missing)")
        return 2
    blob = "\n".join(texts)
    real_to_token = {}
    if a.hermes_home:
        try:
            sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(
                __import__("os").path.abspath(__file__))))
            from hermescloak.egress import load_token_map
            real_to_token = {v: k for k, v in load_token_map(a.hermes_home).items()}
        except Exception:
            real_to_token = {}
    per = collections.defaultdict(lambda: {"expected": "", "values": 0, "leaked": 0, "token": 0})
    for m in manifest:
        row = per[m["type"]]
        row["expected"] = m["expected"]
        row["values"] += 1
        clear = m["value"] in blob
        row["leaked"] += clear
        tok = real_to_token.get(m["value"])
        row["token"] += bool(tok and tok in blob and not clear)
    tokens = blob.count("⟦")
    print(f"scanned {n} model-bound requests; {tokens} token(s) seen")
    print(f"  {'type':30} {'expected':8} {'as token':>9} {'in clear':>9} / values")
    leaks = 0
    for typ, r in sorted(per.items(), key=lambda kv: (kv[1]["expected"] != "masked", kv[0])):
        flag = "LEAK" if r["expected"] == "masked" and r["leaked"] else ("gap " if r["leaked"] else "ok  ")
        print(f"  [{flag}] {typ:24} {r['expected']:8} {r['token']:>9} {r['leaked']:>9} / {r['values']}")
        if r["expected"] == "masked":
            leaks += r["leaked"]
    print(("NO LEAKS of must-mask values ✓" if not leaks else f"{leaks} must-mask value(s) reached the model ✗")
          + " (a value only counts if the conversation actually brought that case to the model)")
    return 1 if leaks else 0


if __name__ == "__main__":
    sys.exit(main())
