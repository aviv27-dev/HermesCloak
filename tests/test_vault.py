from hermescloak.vault import Vault

def test_same_value_same_token():
    v = Vault()
    t1 = v.tokenize("שירה לוי", "לקוח")
    t2 = v.tokenize("שירה לוי", "לקוח")
    assert t1 == t2 == "⟦לקוח_1⟧"

def test_different_values_increment_per_type():
    v = Vault()
    assert v.tokenize("שירה לוי", "לקוח") == "⟦לקוח_1⟧"
    assert v.tokenize("דני כהן", "לקוח") == "⟦לקוח_2⟧"
    assert v.tokenize("0501234567", "טלפון") == "⟦טלפון_1⟧"

def test_restore_roundtrip():
    v = Vault()
    tok = v.tokenize("שירה לוי", "לקוח")
    assert v.restore_token(tok) == "שירה לוי"

def test_restore_unknown_token_returns_none():
    v = Vault()
    assert v.restore_token("⟦לקוח_9⟧") is None

def test_same_value_one_token_regardless_of_type():
    # a value detected first as חפ then later (bare) as מזהה must keep ONE token —
    # otherwise the model sees two tokens for one value and cross-references wrongly
    v = Vault()
    t1 = v.tokenize("222222200", "חפ")
    t2 = v.tokenize("222222200", "מזהה")
    assert t1 == t2 == "⟦חפ_1⟧"

def test_has_entries():
    v = Vault()
    assert v.is_empty()
    v.tokenize("x", "לקוח")
    assert not v.is_empty()
