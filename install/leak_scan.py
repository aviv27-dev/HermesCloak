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
    texts, n, turns = [], 0, 0
    try:
        for line in open(a.log, encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            n += 1
            try:
                obj = json.loads(line)
                turns += isinstance(obj, dict) and bool(obj.get("messages") or obj.get("input"))
                _strings(obj, texts)
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
    print(f"scanned {n} model-bound requests ({turns} carrying a conversation); {tokens} token(s) seen")
    if not turns:
        print("NOTHING TO VERIFY — no conversation reached the model in this window (no chat, or the "
              "messages never reached the agent)")
        return 3
    # identifiers that are NOT in the corpus manifest (e.g. typed by the tester): counts only
    import re
    clear_text = re.sub(r"⟦[^⟧]*⟧", " ", blob)
    nines = re.findall(r"(?<![\d\-./])\d{9}(?![\d\-./])", clear_text)

    def _valid(d):
        t = 0
        for i, ch in enumerate(d):
            x = int(ch) * (1 if i % 2 == 0 else 2)
            t += x if x < 10 else x - 9
        return t % 10 == 0

    phones = re.findall(r"(?<!\d)05\d-?\d{7}(?!\d)", clear_text)
    emails = re.findall(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}", clear_text)
    valid_ids = sum(_valid(d) for d in nines)
    print(f"any identifiers in clear (corpus or typed): 9-digit numbers={len(nines)} "
          f"(valid ID check digit={valid_ids}), mobile phones={len(phones)}, emails={len(emails)}")
    print(f"  {'type':30} {'expected':8} {'as token':>9} {'in clear':>9} / values")
    leaks = 0
    for typ, r in sorted(per.items(), key=lambda kv: (kv[1]["expected"] != "masked", kv[0])):
        flag = "LEAK" if r["expected"] == "masked" and r["leaked"] else ("gap " if r["leaked"] else "ok  ")
        print(f"  [{flag}] {typ:24} {r['expected']:8} {r['token']:>9} {r['leaked']:>9} / {r['values']}")
        if r["expected"] == "masked":
            leaks += r["leaked"]
    leaks_any = leaks + valid_ids + len(phones) + len(emails)
    print(("NO LEAKS of must-mask values ✓" if not leaks_any else
           f"{leaks_any} identifier(s) reached the model in clear ✗ (corpus: {leaks}, any valid ID/phone/email: "
           f"{valid_ids + len(phones) + len(emails)})")
          + " (a value only counts if the conversation actually brought that case to the model)")
    return 1 if (leaks or valid_ids or phones or emails) else 0


if __name__ == "__main__":
    sys.exit(main())
