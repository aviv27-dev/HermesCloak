"""Residual-identifier check: a second opinion on the ALREADY-MASKED request.

Regexes and a client list cannot close two gaps — a first name on its own, and a party whose name
is not on the list (with NER off). A typed-decision model reading the tokenized text can say whether
a private person's name, a contact detail or an identifier is still there in clear. The text sent is
exactly what the cloud model is about to receive anyway (tokens, not values), so the check adds no
new kind of exposure — but it IS a second vendor; it is off unless ``jev_check: true`` and a key.

``check(text, profile)`` → ``{"hits": {q: p}, "probs": {q: p}, "ms", "model", "backend"}`` or None when
the check is off / unconfigured; ``{"error": reason}`` when it did not run (breaker, timeout, cap…) —
the caller then behaves as before. Backend: Jev, or the office's own local decision model
(profile ``decide_backend: local``), optionally with the other one in shadow (``decide_shadow``).
"""
from __future__ import annotations

from hermescloak import decide as _decide

USE_CASE = "residual:request"
# Level B as the office approved it for Jev: at most 600 characters of (masked) text per call. The
# most recent part of the request is where a new leak would be. Raise it only for the local backend.
MAX_CHARS = 600

QUESTIONS = {
    "person": {"type": "noul",
               "instructions": ('האם בטקסט מופיע בגלוי שם של אדם פרטי — לקוח, צד להליך, חייב, עד או נמען — '
                                'שלא הוחלף במציין בצורה ⟦…⟧? שמות של שופטים, עורכי דין, פקידים ומוסדות, '
                                'וכן שמות שכבר מופיעים כמציינים ⟦…⟧, אינם נחשבים.'),
               "criteria": {"true": "כן — שם של אדם פרטי מופיע בגלוי", "false": "לא"}},
    "contact": {"type": "noul",
                "instructions": ('האם מופיעים בגלוי מספר טלפון, כתובת דוא"ל של אדם פרטי או כתובת מגורים '
                                 '(רחוב ומספר בית) שלא הוחלפו במציין ⟦…⟧? כתובות של בתי משפט, משרדי ממשלה '
                                 'ומוסדות אינן נחשבות.'),
                "criteria": {"true": "כן — פרט התקשרות אישי בגלוי", "false": "לא"}},
    "identifier": {"type": "noul",
                   "instructions": ('האם מופיע בגלוי מספר זהות, דרכון, חשבון בנק, כרטיס אשראי או מספר '
                                    'רישוי של אדם פרטי שלא הוחלף במציין ⟦…⟧? מספרי תיק, סכומים ותאריכים '
                                    'אינם נחשבים.'),
                   "criteria": {"true": "כן — מזהה אישי בגלוי", "false": "לא"}},
}


def check(text: str, profile, decider: _decide.Decider | None = None, on_shadow=None) -> dict | None:
    if not getattr(profile, "jev_check", False):
        return None
    d = decider or _decide.shared()
    backend = getattr(profile, "decide_backend", None) or None
    if not d.configured(backend):
        return None
    text = text or ""
    cap = int(getattr(profile, "jev_max_chars", MAX_CHARS) or MAX_CHARS)
    if len(text) > cap:
        text = text[-cap:]
    r = d.decide(USE_CASE, {"text": text}, QUESTIONS, timeout_s=getattr(profile, "jev_timeout_s", None),
                 backend=backend, shadow=bool(getattr(profile, "decide_shadow", False)), on_shadow=on_shadow)
    if not r.get("ok"):
        return {"error": r.get("reason"), "detail": r.get("error"), "ms": r.get("ms"),
                "announce": r.get("announce", False), "backend": r.get("backend")}
    probs = {}
    for name, a in (r.get("answers") or {}).items():
        try:
            probs[name] = round(float(a.get("noul")), 3)
        except (TypeError, AttributeError, ValueError):
            continue
    thr = float(getattr(profile, "jev_min_confidence", 0.7))
    hits = {k: v for k, v in probs.items() if v >= thr}
    return {"hits": hits, "probs": probs, "ms": r.get("ms"), "model": r.get("model"),
            "backend": r.get("backend"), "fell_back_from": r.get("fell_back_from")}
