from hermescloak.vault import Vault
from hermescloak.restorer import restore_text, restore_json, leftover_tokens

def loaded_vault():
    v = Vault()
    v.tokenize("שירה לוי", "לקוח")     # ⟦לקוח_1⟧
    v.tokenize("a@b.co.il", "מייל")    # ⟦מייל_1⟧
    return v

def test_restore_text():
    v = loaded_vault()
    assert restore_text("שלום ⟦לקוח_1⟧", v) == "שלום שירה לוי"

def test_restore_json_nested_tool_args():
    v = loaded_vault()
    args = {"to": "⟦מייל_1⟧", "body": "עבור ⟦לקוח_1⟧", "cc": ["⟦מייל_1⟧"]}
    out = restore_json(args, v)
    assert out == {"to": "a@b.co.il", "body": "עבור שירה לוי", "cc": ["a@b.co.il"]}

def test_leftover_detects_unrestored():
    v = loaded_vault()
    assert leftover_tokens("⟦לקוח_1⟧ ⟦טלפון_9⟧", v) == ["⟦טלפון_9⟧"]

def test_leftover_empty_when_all_known_restored():
    v = loaded_vault()
    assert leftover_tokens(restore_text("⟦לקוח_1⟧", v), v) == []


def test_bare_label_restored_only_as_a_whole_issued_token():
    """GLM / gpt-5.x sometimes answer with the brackets stripped ("לקוח_1"); pii.js restores those."""
    from hermescloak.vault import Vault
    from hermescloak.restorer import restore_text
    v = Vault()
    tok = v.tokenize("דנה לוי", "לקוח")            # ⟦לקוח_1⟧
    bare = tok[1:-1]
    text = f"מאת {bare} אל {bare}. x{bare} {bare}2 לקוח_9 מייל_1 {tok}"
    assert restore_text(text, v, tolerant=True) == "מאת דנה לוי אל דנה לוי. xלקוח_1 לקוח_12 לקוח_9 מייל_1 דנה לוי"
    assert restore_text(bare, v, tolerant=False) == bare           # default: canonical tokens only
    assert restore_text(bare, v, tolerant="strict") == bare        # tool arguments: never
