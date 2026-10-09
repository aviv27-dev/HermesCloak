"""Coverage on a FICTIONAL corpus of Israeli-style court decisions (tests/fixtures/verdicts).

Every value the manifest marks "masked" must be gone from the tokenized text, and restoring the
tokenized text must give back the original document byte-for-byte. "gap" values (a first name on
its own; a name missing from the client list with NER off) are measured and printed, not failed.
"""
import collections
import json
import os

from hermescloak.engine import Engine
from hermescloak.entities import StaticFileSource
from hermescloak.profile import Profile
from hermescloak.restorer import restore_text

D = os.path.join(os.path.dirname(__file__), "fixtures", "verdicts")


def _engine():
    return Engine(Profile(name="corpus"), entity_source=StaticFileSource(os.path.join(D, "gazetteer.txt")))


def _cases():
    out = {}
    for name in sorted(os.listdir(os.path.join(D, "cases"))):
        if name.startswith("case_"):
            out[int(name[5:7])] = open(os.path.join(D, "cases", name), encoding="utf-8").read()
    return out


def test_every_must_mask_value_is_masked_and_restores_exactly(capsys):
    eng = _engine()
    manifest = json.load(open(os.path.join(D, "manifest.json"), encoding="utf-8"))
    originals = _cases()
    sent = {n: eng.sanitize_outbound([{"role": "user", "content": t}])[-1]["content"]
            for n, t in originals.items()}
    leaks = [(m["case"], m["type"]) for m in manifest
             if m["expected"] == "masked" and m["value"] in sent[m["case"]]]
    assert not leaks, leaks
    for n, t in originals.items():
        assert restore_text(sent[n], eng.vault) == t
    gaps = collections.Counter(m["type"] for m in manifest
                               if m["expected"] == "gap" and m["value"] in sent[m["case"]])
    with capsys.disabled():
        print(f"\n[verdict corpus] {len(manifest)} values; must-mask leaks: 0; known gaps leaked: {dict(gaps)}")


def test_new_detectors_do_not_fire_on_ordinary_text():
    from hermescloak.recognizers.deterministic import DeterministicRecognizer
    d = DeterministicRecognizer()
    for text in ["בדרך כלל 5 פעמים", "בסך 5,000 ש\"ח 12345", "מחיר 12-345 ש\"ח", "שעה 10-30",
                 "בעמוד 12 שורה 4", "פסקה 7 לחוות הדעת"]:
        assert not d.recognize(text), (text, d.recognize(text))


def test_must_mask_values_masked_inside_json_escaped_tool_results():
    """Tool results are often JSON text: quotes arrive as \\" and newlines as a literal \\n. The
    end-to-end Telegram run found ת\\"פ case numbers leaking this way."""
    eng = _engine()
    manifest = json.load(open(os.path.join(D, "manifest.json"), encoding="utf-8"))
    for n, text in _cases().items():
        payload = json.dumps({"path": f"case_{n:02d}.md", "content": text}, ensure_ascii=False)
        sent = eng.sanitize_outbound([{"role": "tool", "content": payload}])[-1]["content"]
        leaks = [m["type"] for m in manifest
                 if m["case"] == n and m["expected"] == "masked" and m["value"] in sent]
        assert not leaks, (n, leaks)
        assert restore_text(sent, eng.vault) == payload


def test_name_at_start_of_escaped_line_is_masked():
    from hermescloak.entities import CallableSource
    from hermescloak.recognizers.gazetteer import GazetteerRecognizer
    from hermescloak.detection import DetectionEngine
    det = DetectionEngine([GazetteerRecognizer(CallableSource(lambda: [("איתי ברקוביץ", "לקוח")]))])
    spans = det.detect('{"content": "שורה\\nאיתי ברקוביץ הגיע"}')
    assert [s.text for s in spans] == ["איתי ברקוביץ"]
