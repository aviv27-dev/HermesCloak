#!/usr/bin/env python3
"""Shadow report: how the two decision backends agreed while one ran in shadow (profile
``decide_shadow: true``). Reads ``decide_shadow`` events from the audit log — probabilities only,
never text — and prints, per question, agreement at the production threshold and the shadow
backend's latency. Use it before switching ``decide_backend``.

  python install/decide_shadow_report.py [--hermes-home DIR] [--threshold 0.7]
"""
import argparse
import glob
import json
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hermes-home", default=None)
    ap.add_argument("--threshold", type=float, default=0.7)
    a = ap.parse_args()
    if a.hermes_home:
        os.environ["HERMES_HOME"] = a.hermes_home
    from hermescloak.adapter.hermes_live import _home
    base = os.path.join(_home(), "cloak", "audit.log")
    rows, no_shadow, ms = [], 0, []
    for path in sorted(glob.glob(base + "*")):
        for line in open(path, encoding="utf-8"):
            try:
                e = json.loads(line)
                if e.get("kind") != "decide_shadow":
                    continue
                r = json.loads(e["detail"])
            except Exception:
                continue
            if not r.get("shadow_answers"):
                no_shadow += 1
                continue
            rows.append(r)
            if r.get("shadow_ms") is not None:
                ms.append(r["shadow_ms"])
    if not rows:
        print(f"no decide_shadow events with a shadow answer in {base}* ({no_shadow} without one)")
        return 2
    primary, shadow = rows[0]["primary"], rows[0]["shadow"]
    print(f"{len(rows)} shadowed decisions · primary {primary} · shadow {shadow} · "
          f"{no_shadow} with no shadow answer · shadow latency median {int(statistics.median(ms)) if ms else '?'} ms")
    print(f"  {'question':14} {'n':>5} {'agree@thr':>10} {'only primary':>13} {'only shadow':>12}  (threshold {a.threshold})")
    for q in sorted({k for r in rows for k in (r.get("primary_answers") or {})}):
        n = agree = only_p = only_s = 0
        for r in rows:
            p, s = (r.get("primary_answers") or {}).get(q), (r.get("shadow_answers") or {}).get(q)
            if not p or not s:
                continue
            n += 1
            if "c" in p:                                   # choice: same option
                agree += p["c"] == s["c"]
                only_p += p["c"] != s["c"] and p["p"] >= a.threshold
                only_s += p["c"] != s["c"] and s["p"] >= a.threshold
            else:                                          # noul: same side of the threshold
                hp, hs = p["p"] >= a.threshold, s["p"] >= a.threshold
                agree += hp == hs
                only_p += hp and not hs
                only_s += hs and not hp
        if n:
            print(f"  {q:14} {n:>5} {100 * agree / n:>9.1f}% {only_p:>13} {only_s:>12}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
