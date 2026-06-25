from hermescloak.recognizers.deterministic import DeterministicRecognizer

R = DeterministicRecognizer()

def types(text):
    return sorted({s.entity_type for s in R.recognize(text)})

def test_valid_israeli_id_detected():
    # 123456709 is a valid Israeli ID (passes check-digit)
    spans = [s for s in R.recognize("ת.ז. 123456709 שלי") if s.entity_type == "תז"]
    assert len(spans) == 1
    assert spans[0].text == "123456709"

def test_invalid_israeli_id_not_detected_as_id():
    # 123456789 fails the check digit
    assert not [s for s in R.recognize("123456789") if s.entity_type == "תז"]

def test_email_phone_detected():
    t = "מייל a@b.co.il טלפון 050-1234567"
    assert "מייל" in types(t)
    assert "טלפון" in types(t)

def test_case_number_detected():
    spans = [s for s in R.recognize('פש"ר 100200300 בתיק') if s.entity_type == "תיק"]
    assert spans and "100200300" in spans[0].text

def test_credit_card_luhn():
    # 4111111111111111 passes Luhn (contiguous)
    spans = [s for s in R.recognize("כרטיס 4111111111111111") if s.entity_type == "אשראי"]
    assert len(spans) == 1

def test_credit_card_grouped_format_detected():
    spans = [s for s in R.recognize("כרטיס 4111 1111 1111 1111") if s.entity_type == "אשראי"]
    assert len(spans) == 1

def test_card_no_false_positive_on_digit_by_digit_spacing():
    # innocent spaced digits (e.g. a number-heavy DB dump) must NOT be read as a card —
    # this is the 52-false-matches regression: the old regex spanned these into a Luhn-valid 16
    txt = "ערכים 4 2 4 2 4 2 4 2 4 2 4 2 4 2 4 2 בטבלה"
    assert [s for s in R.recognize(txt) if s.entity_type == "אשראי"] == []

def test_card_no_false_positive_on_mixed_separated_numbers():
    txt = "טלפון 050-1234567 פקס 03-7654321 חשבון 12-345-678 קוד 11 22 33 44 55 66"
    assert [s for s in R.recognize(txt) if s.entity_type == "אשראי"] == []

def test_labeled_company_number_with_separators():
    # 123456709 is a valid 9-digit check-digit; shown with hyphens after a ח.פ label
    spans = [s for s in R.recognize("ח.פ 12-345670-9 של החברה") if s.entity_type == "חפ"]
    assert len(spans) == 1 and spans[0].text == "12-345670-9"

def test_labeled_id_with_colon():
    spans = [s for s in R.recognize("ת.ז: 123456709 שלי") if s.entity_type == "תז"]
    assert spans and "123456709" in spans[0].text

def test_separator_id_caught_even_far_from_label():
    # 123456709 valid; label NOT adjacent ("ח.פ של החברה הוא 12-345670-9")
    spans = R.recognize("ח.פ של החברה הוא 12-345670-9")
    assert any(s.text == "12-345670-9" for s in spans)

def test_separated_number_not_an_id_is_ignored():
    # 123456789 fails the check digit even with separators
    assert not [s for s in R.recognize("מספר 12-345678-9") if s.entity_type in {"תז", "חפ"}]

def test_valid_9digit_id_starting_with_zero_not_labeled_phone():
    # 020000006 is a valid 9-digit ID; phone regex would grab it, but it must be an ID label
    spans = R.recognize("ת.ז 020000006")
    assert "טלפון" not in {s.entity_type for s in spans}
    assert any(s.entity_type in {"תז", "חפ"} and "020000006" in s.text for s in spans)

def test_real_phone_still_detected():
    assert [s for s in R.recognize("טלפון 050-1234567") if s.entity_type == "טלפון"]

def test_bare_id_uses_neutral_type_not_false_assertion():
    # no adjacent label -> ambiguous (ת"ז vs ח.פ) -> neutral ⟦מזהה⟧, never a wrong type
    spans = [s for s in R.recognize("המספר 123456709 ברשומה") if s.text == "123456709"]
    assert spans and spans[0].entity_type == "מזהה"

def test_labeled_id_keeps_accurate_type():
    assert any(s.entity_type == "חפ" for s in R.recognize("ח.פ 123456709"))
    assert any(s.entity_type == "תז" for s in R.recognize("ת.ז 123456709"))

def test_voip_and_geographic_phones_detected():
    # regression: 07x VoIP + 2-digit areas were missed by the old [2-489] area class
    for p in ["0747654321", "077-1234567", "073 1234567", "08-1112222"]:
        assert [s for s in R.recognize(f"טלפון {p}") if s.entity_type == "טלפון"], p

def test_gush_helka_type_has_no_underscore():
    # regression: a type with '_' breaks the ⟦type_n⟧ grammar -> unrestorable token
    sp = [s for s in R.recognize("הנכס בגוש 6941 חלקה 21") if "גוש" in s.entity_type]
    assert sp and "_" not in sp[0].entity_type
