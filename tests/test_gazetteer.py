from hermescloak.entities import CallableSource
from hermescloak.recognizers.gazetteer import GazetteerRecognizer

def make(names):
    return GazetteerRecognizer(CallableSource(lambda: names))

def test_exact_match():
    r = make([("שירה לוי", "לקוח")])
    spans = r.recognize("פגשתי את שירה לוי אתמול")
    assert len(spans) == 1 and spans[0].text == "שירה לוי" and spans[0].entity_type == "לקוח"

def test_order_independent_surname_first():
    r = make([("שירה לוי", "לקוח")])      # DB form given-first
    spans = r.recognize("לוי שירה הגישה בקשה")  # text surname-first
    assert len(spans) == 1

def test_proclitic_prefix_stripped():
    r = make([("שירה לוי", "לקוח")])
    spans = r.recognize("שלחתי לשירה לוי מכתב")  # "ל" glued to first token
    assert len(spans) == 1

def test_no_false_match_on_partial():
    r = make([("שירה לוי", "לקוח")])
    assert r.recognize("אלה דברים אחרים") == []
