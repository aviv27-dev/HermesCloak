from hermescloak.entities import CallableSource
from hermescloak.recognizers.deterministic import DeterministicRecognizer
from hermescloak.recognizers.gazetteer import GazetteerRecognizer
from hermescloak.detection import DetectionEngine
from hermescloak.vault import Vault
from hermescloak.pseudonymizer import pseudonymize

def eng():
    return DetectionEngine([DeterministicRecognizer(),
                            GazetteerRecognizer(CallableSource(lambda: [("שירה לוי", "לקוח")]))])

def test_replaces_spans_with_tokens():
    v = Vault()
    out = pseudonymize("שירה לוי בטלפון 050-1234567", eng(), v)
    assert "שירה לוי" not in out and "050-1234567" not in out
    assert "⟦לקוח_1⟧" in out and "⟦טלפון_1⟧" in out

def test_coreference_same_token():
    v = Vault()
    out = pseudonymize("שירה לוי ועוד שירה לוי", eng(), v)
    assert out.count("⟦לקוח_1⟧") == 2

def test_no_pii_unchanged():
    v = Vault()
    assert pseudonymize("טקסט ללא מידע רגיש", eng(), v) == "טקסט ללא מידע רגיש"
