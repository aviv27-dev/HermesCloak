from hermescloak.tokens import make_token, find_tokens, TOKEN_RE

def test_make_token():
    assert make_token("לקוח", 1) == "⟦לקוח_1⟧"
    assert make_token("תז", 12) == "⟦תז_12⟧"

def test_find_tokens_returns_all_placeholders():
    text = "שלום ⟦לקוח_1⟧, התיק ⟦תיק_2⟧ של ⟦לקוח_1⟧"
    assert find_tokens(text) == ["⟦לקוח_1⟧", "⟦תיק_2⟧", "⟦לקוח_1⟧"]

def test_find_tokens_empty_when_none():
    assert find_tokens("no placeholders here") == []

def test_token_re_matches_hebrew_and_ascii_types():
    assert TOKEN_RE.findall("⟦EMAIL_3⟧ ⟦טלפון_4⟧") == [("EMAIL", "3"), ("טלפון", "4")]

def test_make_token_strips_underscore_in_type():
    # a '_' in the type would break the ⟦type_n⟧ grammar -> unparseable/unrestorable
    t = make_token("גוש_חלקה", 1)
    assert t == "⟦גושחלקה_1⟧" and find_tokens(t) == [t]
