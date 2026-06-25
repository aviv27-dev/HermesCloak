"""The ~100 QA cases as browsable DATA + a live per-example runner.

Ported from the cloak-qa harness so the demo page can step through every case,
show its live PASS/FAIL, and re-run it on demand. Synthetic data only.

Each example: {id, category, text, mode, ...}. `run_example(ex)` builds a FRESH
engine (so cases are independent), runs the real sanitize->restore lifecycle, and
returns {outbound, restored, verdict: pass|fail|info, detail}. NER is wired in
fail-open: if the microservice is down, NER-dependent cases just show that.
"""
import os
import re

from hermescloak import Engine, Profile, CallableSource
from hermescloak.adapter.ner_client import NerServiceRecognizer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROFILE_PATH = os.path.join(ROOT, "profiles", "example.yaml")
NER_URL = "http://127.0.0.1:8011"

# Gazetteer used by the demo engine — includes every name the gaz_name cases use.
GAZ = [(n, "לקוח") for n in (
    "שירה לוי", "דוד מזרחי", "חברת אורות בעמ", "יוסי בן דוד",
    "דנה כהן", "יוסי לוי", "רונית אברהם", "משה פרץ",
)]
NEVER = ["בית המשפט", "לשכת ההוצאה לפועל", "כונס הנכסים הרשמי"]
_TOK = re.compile(r"⟦[^⟧]+⟧")


def _valid_id(n: int) -> bool:
    s = f"{n:09d}"
    t = 0
    for i, c in enumerate(s):
        x = int(c) * (1 if i % 2 == 0 else 2)
        t += x if x < 10 else x - 9
    return t % 10 == 0


VIDS = [f"{n:09d}" for n in range(100000000, 100002000) if _valid_id(n)][:12]


def build_engine(with_ner: bool = True) -> Engine:
    recs = []
    if with_ner:
        try:
            recs = [NerServiceRecognizer(NER_URL, timeout=20)]
        except Exception:
            recs = []
    prof = Profile.from_yaml(PROFILE_PATH) if os.path.exists(PROFILE_PATH) else Profile(name="demo", never_mask=NEVER)
    return Engine(profile=prof, entity_source=CallableSource(lambda: GAZ), extra_recognizers=recs)


def _build_examples():
    ex, n = [], 0

    def add(cat, text, mode, **kw):
        nonlocal n
        n += 1
        ex.append({"id": n, "category": cat, "text": text, "mode": mode, **kw})

    # 1 bare valid IDs
    for v in VIDS[:10]:
        add("bare_id", f"המספר שלי הוא {v} ברשומה", "mask", targets=[v])
    # 2 invalid IDs must NOT be masked as an id
    for bad in ["123456789", "111111111", "000000001", "999999999", "123123123"]:
        add("invalid_id_not_masked", f"מספר {bad}", "invalid_id", value=bad)
    # 3 labeled ת"ז
    for v in VIDS[:5]:
        add("labeled_tz", f'ת"ז {v}', "labeled", value=v, token="תז")
    # 4 labeled ח.פ
    for v in VIDS[:5]:
        add("labeled_hp", f'ח.פ {v}', "labeled", value=v, token="חפ")
    # 5 IDs with separators
    for v in VIDS[:4]:
        sep = f"{v[:2]}-{v[2:8]}-{v[8]}"
        add("id_sep_labeled", f"ח.פ {sep} של החברה", "mask", targets=[sep])
        add("id_sep_bare", f"הרשומה מכילה {sep} כמזהה", "mask", targets=[sep])
    # 6 phones
    for p in ["050-1234567", "0521234567", "+972-54-1234567", "03-1234567", "09 8887777",
              "054-765-4321", "0747654321", "02-6543210", "+972502223344", "08-1112222"]:
        add("phone", f"טלפון {p} ליצירת קשר", "mask", targets=[p])
    # 7 emails
    for em in ["a@b.co.il", "test.user@example.com", "yossi+tag@firm.org", "x_y@sub.domain.io", "info@a.co"]:
        add("email", f"כתובת מייל {em}", "mask", targets=[em])
    # 8 real credit cards
    for cc in ["4111111111111111", "4111 1111 1111 1111", "5500005555555559", "4012 8888 8888 1881"]:
        add("credit_real", f"כרטיס {cc}", "mask", targets=[cc])
    # 9 credit-card false positives
    for fp in ["4 2 4 2 4 2 4 2 4 2 4 2 4 2 4 2", "11 22 33 44 55 66 77 88",
               "050 1234567 03 7654321 02 9998888", "1 2 3 4 5 6 7 8 9 0 1 2 3 4",
               "תאריכים 2024 2025 2026 2027 2028 2029", "סכומים 100 200 300 400 500 600 700"]:
        add("credit_false_pos", fp, "no_credit")
    # 10 case numbers
    for cz in ['פש"ר 12345', 'חדל"פ 678901', 'הוצל"פ 555444', 'ת"א 9999', 'ה"פ 1234567', 'תיק 4321', 'פשר 222333']:
        num = "".join(ch for ch in cz if ch.isdigit())
        add("case_no", f"בעניין {cz} שהוגש", "mask", targets=[num])
    # 11 gush/helka
    for g in ["גוש 6941 חלקה 21", "גוש 30255 חלקה 7", "גוש 100 חלקה 2"]:
        add("gush_helka", f"הנכס ב{g}", "mask", targets=[g])
    # 12 gazetteer names (exact / surname-first / proclitic)
    for variant in ["שירה לוי", "לוי שירה", "לשירה לוי", "בדוד מזרחי", "ליוסי בן דוד", "דוד מזרחי", "חברת אורות בעמ", "ושירה לוי"]:
        tgt = variant.lstrip("לבוהמ") if variant[0] in "לבוהמ" and variant != "לוי שירה" else variant
        add("gaz_name", f"פגשתי את {variant} בתיק", "mask", targets=[tgt])
    # 13 NER names (live model)
    for nm in ["דנה אברהמי", "מירי שטרן", "אבי רוזן", "תמר", "רון", "נועה פרידמן"]:
        add("ner_name", f"הלקוח {nm} הגיש בקשה", "ner", value=nm)
    # 14 English names (known gap)
    for en in ["John Doe", "Jane Smith", "Acme Corp", "Robert Wilson"]:
        add("english_name", f"client {en} called", "note", value=en,
            note="ידוע: אין NER אנגלי — עובר ללא טשטוש")
    # 15 coref same token
    add("coref_same_token", "שירה לוי וגם שירה לוי שוב", "coref", n_distinct=1, n_tokens=2)
    # 16 coref value-keyed (labeled then bare same id)
    add("coref_value_keyed", f'ח.פ {VIDS[0]} ובהמשך {VIDS[0]} שוב', "coref", n_distinct=1)
    # 17 tool-call args restore
    add("toolcall_restore", "מייל לשירה לוי a@b.co.il", "toolcall")
    # 18 leftover detection
    add("leftover_flagged", "שירה לוי", "leftover")
    # 19 no-PII
    for t in ["שלום עולם מה שלומך", "תודה רבה על העזרה", "פגישה מחר בעשר"]:
        add("no_pii", t, "no_mask")
    # 20 never-mask allowlist
    for t in ["הוגש לבית המשפט היום", "פנייה ללשכת ההוצאה לפועל", "בית המשפט המחוזי"]:
        add("never_mask", t, "no_mask")
    # 21 mixed multi-entity
    add("mixed_multi", 'הלקוח שירה לוי, ת"ז ' + VIDS[1] + ', טלפון 050-1234567, מייל a@b.co.il',
        "mask", targets=["שירה לוי", VIDS[1], "050-1234567", "a@b.co.il"])
    for i in range(4):
        add("mixed_multi", f'דוד מזרחי בטלפון 052-111{i}222 ומייל u{i}@x.co.il', "mask",
            targets=["דוד מזרחי", f"052-111{i}222", f"u{i}@x.co.il"])
    # 22 robustness
    add("robust_long", ("שירה לוי " * 50) + "טלפון 050-1234567", "mask", targets=["050-1234567"])
    add("robust_unicode", "לקוח 😀 שירה לוי ✓ טלפון 050-1234567", "mask", targets=["שירה לוי", "050-1234567"])
    add("robust_numbers_in_words", "יש לי 3 ילדים ו2 כלבים", "no_mask")
    add("robust_idlike_in_text", "החשבון 100000000 פעיל", "mask", targets=[])
    return ex


EXAMPLES = _build_examples()


def run_example(ex: dict) -> dict:
    """Run one example live against a fresh engine. Returns outbound/restored/verdict/detail."""
    eng = build_engine()
    text = ex["text"]
    mode = ex["mode"]
    out = eng.sanitize_outbound([{"role": "user", "content": text}])
    user_out = [m for m in out if m["role"] == "user"][-1]["content"]
    blob = " ".join(m.get("content", "") for m in out)

    def restored_of(tok_text):
        r, rep = eng.restore_inbound({"content": tok_text, "tool_calls": []})
        return r["content"], rep

    if mode == "no_mask":
        ok = "⟦" not in user_out
        return _v(user_out, user_out, "pass" if ok else "fail",
                  "" if ok else "טושטש שלא לצורך", ex)

    if mode == "invalid_id":
        bad = ex["value"]
        ok = ("⟦תז" not in user_out) and ("⟦חפ" not in user_out)
        return _v(user_out, user_out, "pass" if ok else "fail",
                  "" if ok else f"מספר לא-תקין טושטש כמזהה: {bad}", ex)

    if mode == "no_credit":
        ok = "⟦אשראי" not in user_out
        return _v(user_out, user_out, "pass" if ok else "fail",
                  "" if ok else "זוהה בטעות ככרטיס אשראי", ex)

    if mode == "labeled":
        v, token = ex["value"], ex["token"]
        masked = (v not in user_out) and (f"⟦{token}_" in user_out)
        rtext, rep = restored_of(user_out)
        ok = masked and (v in rtext) and not rep.leftover
        return _v(user_out, rtext, "pass" if ok else "fail",
                  "" if ok else f"ציפינו ל-⟦{token}_⟧ ושחזור ל-{v}", ex)

    if mode == "ner":
        nm = ex["value"]
        ok = (nm not in user_out) and ("⟦" in user_out)
        return _v(user_out, user_out, "pass" if ok else "fail",
                  "" if ok else f"ה-NER לא זיהה את השם: {nm}", ex)

    if mode == "note":
        passed_through = ex["value"] in user_out
        return _v(user_out, user_out, "info",
                  ("עבר ללא טשטוש (פער ידוע)" if passed_through else "זוהה"), ex)

    if mode == "coref":
        toks = _TOK.findall(user_out)
        ok = len(set(toks)) == ex.get("n_distinct", 1)
        if "n_tokens" in ex:
            ok = ok and len(toks) == ex["n_tokens"]
        return _v(user_out, user_out, "pass" if ok else "fail",
                  "" if ok else f"tokens={toks}", ex)

    if mode == "toolcall":
        resp = {"content": "שולח", "tool_calls": [{"function": {"name": "send_email",
                "arguments": {"to": "⟦מייל_1⟧", "cc": ["⟦מייל_1⟧"], "body": "עבור ⟦לקוח_1⟧"}}}]}
        r, rep = eng.restore_inbound(resp)
        args = r["tool_calls"][0]["function"]["arguments"]
        ok = args["to"] == "a@b.co.il" and args["body"] == "עבור שירה לוי" and not rep.leftover
        import json as _j
        return _v(user_out, _j.dumps(args, ensure_ascii=False), "pass" if ok else "fail",
                  "" if ok else "שחזור ארגומנטים נכשל", ex)

    if mode == "leftover":
        r, rep = eng.restore_inbound({"content": "⟦לקוח_1⟧ ו⟦טלפון_9⟧", "tool_calls": []})
        ok = "⟦טלפון_9⟧" in rep.leftover
        return _v(user_out, r["content"], "pass" if ok else "fail",
                  f"leftover={rep.leftover}" if ok else "לא דווח leftover על טוקן לא מוכר", ex)

    # default: mode == "mask"
    targets = ex.get("targets", [])
    leaks = [t for t in targets if t in blob]
    rtext, rep = restored_of(user_out)
    missing = [t for t in targets if t not in rtext]
    ok = (not leaks) and (not missing) and not rep.leftover
    detail = ""
    if leaks:
        detail += f"דליפה={leaks} "
    if missing:
        detail += f"שחזור-חסר={missing} "
    if rep.leftover:
        detail += f"leftover={rep.leftover}"
    return _v(user_out, rtext, "pass" if ok else "fail", detail, ex)


def _v(outbound, restored, verdict, detail, ex):
    return {"id": ex["id"], "category": ex["category"], "text": ex["text"],
            "outbound": outbound, "restored": restored, "verdict": verdict, "detail": detail}


def examples_meta():
    """Lightweight list for the page nav (no live run)."""
    return [{"id": e["id"], "category": e["category"], "text": e["text"], "mode": e["mode"]} for e in EXAMPLES]
