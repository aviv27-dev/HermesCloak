#!/usr/bin/env python3
"""Measure the Jev second opinion (profile ``jev_check``) on the FICTIONAL verdict corpus.

For every case the document is tokenized by the engine and the MASKED text is shown to Jev with the
three residual questions (person / contact / identifier still in clear). Two runs per case:

  default    the corpus client list as shipped: cases 2 and 5 have a party NOT on the list, and every
             case has a first name on its own → a correct reader should say "person: yes"
  complete   the missing names and first names added to the list → nothing is left in clear → a
             correct reader should say "no" to everything (this run measures false positives)

Needs OPENROUTER_API_KEY (or HERMESCLOAK_JEV_KEY) in the environment — set it in the shell, never in
a chat. Prints probabilities only; no document text and no key is ever printed. Exit 0 when the
default run flags every known gap and the complete run flags nothing above the threshold.

  OPENROUTER_API_KEY=... python install/jev_corpus_check.py [--threshold 0.7]
"""
import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hermescloak import decide, residual                       # noqa: E402
from hermescloak.engine import Engine                         # noqa: E402
from hermescloak.entities import CallableSource               # noqa: E402
from hermescloak.profile import Profile                       # noqa: E402

D = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tests", "fixtures", "verdicts")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--threshold", type=float, default=0.7)
    a = ap.parse_args()
    d = decide.shared()
    if not d.configured():
        print("set OPENROUTER_API_KEY (or HERMESCLOAK_JEV_KEY) in the environment — never in a chat")
        return 2
    manifest = json.load(open(os.path.join(D, "manifest.json"), encoding="utf-8"))
    names = [(l.split("\t")[0], "לקוח") for l in open(os.path.join(D, "gazetteer.txt"), encoding="utf-8")
             if l.strip()]
    complete = names + [(m["value"], "לקוח") for m in manifest if m["expected"] == "gap"]
    prof = Profile(name="jev-measure", jev_check=True, jev_min_confidence=a.threshold)
    cases = sorted(f for f in os.listdir(os.path.join(D, "cases")) if f.startswith("case_"))
    print(f"model {d.model()} · threshold {a.threshold} · {len(cases)} cases × 2 runs")
    print(f"  {'case':8} {'run':9} {'person':>7} {'contact':>8} {'identifier':>11}  verdict")
    ok = True
    for f in cases:
        n = int(re.search(r"\d+", f).group(0))
        text = open(os.path.join(D, "cases", f), encoding="utf-8").read()
        for run, src in (("default", names), ("complete", complete)):
            eng = Engine(prof, entity_source=CallableSource(lambda s=src: s))
            masked = eng.sanitize_outbound([{"role": "user", "content": text}])[-1]["content"]
            r = residual.check(masked, prof, d)
            if not r or "error" in r:
                print(f"  {n:<8} {run:9} — {r.get('error') if r else 'no result'}")
                ok = False
                continue
            p = r["probs"]
            gaps_left = [m for m in manifest if m["case"] == n and m["expected"] == "gap" and m["value"] in masked]
            expect_person = bool(gaps_left)
            got_person = p.get("person", 0) >= a.threshold
            good = (got_person == expect_person) and (run == "default" or not r["hits"])
            ok &= good
            print(f"  {n:<8} {run:9} {p.get('person', 0):7.2f} {p.get('contact', 0):8.2f} {p.get('identifier', 0):11.2f}"
                  f"  {'✓' if good else '✗'} expected person={'yes' if expect_person else 'no'}"
                  f"{' (' + str(len(gaps_left)) + ' name(s) in clear)' if gaps_left else ''}")
    h = d.health()
    print(f"calls {h['calls']} · errors {h['errors']} · breaker {'open' if h['breaker_open'] else 'closed'}")
    print("JEV CHECK: " + ("catches every known gap, no false positives ✓" if ok else "see ✗ rows"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
