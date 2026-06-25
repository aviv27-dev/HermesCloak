from hermescloak.entities import CallableSource
from hermescloak.recognizers.deterministic import DeterministicRecognizer
from hermescloak.recognizers.gazetteer import GazetteerRecognizer
from hermescloak.detection import DetectionEngine

def build(names, never=None):
    return DetectionEngine(
        recognizers=[DeterministicRecognizer(), GazetteerRecognizer(CallableSource(lambda: names))],
        never_mask=never or [],
    )

def test_combines_recognizers_sorted():
    eng = build([("שירה לוי", "לקוח")])
    spans = eng.detect("שירה לוי בטלפון 050-1234567")
    assert [s.entity_type for s in spans] == ["לקוח", "טלפון"]
    assert all(spans[i].end <= spans[i + 1].start for i in range(len(spans) - 1))

def test_never_mask_drops_span():
    eng = build([("בית המשפט", "מוסד")], never=["בית המשפט"])
    assert eng.detect("הוגש לבית המשפט") == []

def test_overlap_longest_wins():
    eng = build([("שירה לוי", "לקוח")])
    spans = eng.detect("שירה לוי")
    assert len(spans) == 1 and spans[0].text == "שירה לוי"

def test_secondary_yields_to_primary_on_overlap():
    # NER-style secondary recognizer returns a longer span incl. a glued proclitic ("לשירה לוי");
    # the curated gazetteer span ("שירה לוי", proclitic-stripped) must win.
    from hermescloak.span import Span
    class FakeNER:
        def recognize(self, text):
            i = text.find("לשירה לוי")
            return [Span(i, i + len("לשירה לוי"), "לקוח", "לשירה לוי")] if i >= 0 else []
    eng = DetectionEngine(
        [DeterministicRecognizer(), GazetteerRecognizer(CallableSource(lambda: [("שירה לוי", "לקוח")]))],
        secondary=[FakeNER()],
    )
    spans = eng.detect("מייל לשירה לוי")
    texts = [s.text for s in spans]
    assert "שירה לוי" in texts and "לשירה לוי" not in texts
