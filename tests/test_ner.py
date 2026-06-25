import pytest
pytest.importorskip("transformers")
from hermescloak.recognizers.ner import HebrewNerRecognizer

def test_hebrew_ner_detects_person_when_model_present():
    try:
        r = HebrewNerRecognizer()  # downloads dicta-il/dictabert-ner on first use
    except Exception as e:
        pytest.skip(f"model unavailable: {e}")
    spans = r.recognize("דנה לוי הגישה בקשה")
    assert any(s.entity_type == "לקוח" for s in spans)
