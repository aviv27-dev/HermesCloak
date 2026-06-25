"""Egress-side restore: catches tokens in outbound content even when the model
built them programmatically (the chr()-construction bug). Reads the on-disk vault."""
import json
import os

from hermescloak.egress import restore_content, restore_file, load_token_map
from hermescloak.durable_vault import DurableVault


def _seed_vault(home, mappings):
    """Write a per-agent vault file the way the live adapter would."""
    vdir = os.path.join(home, "cloak", "vaults")
    os.makedirs(vdir, exist_ok=True)
    v = DurableVault(DurableVault.path_for(vdir, home))
    for real, etype in mappings:
        v.tokenize(real, etype)
    v.save()
    return v


def test_restores_literal_tokens_from_disk_vault(tmp_path):
    home = str(tmp_path)
    v = _seed_vault(home, [("100000009", "מזהה"), ("100000034", "מזהה")])
    t_id = v.tokenize("100000009", "מזהה")          # = its stable token
    body = f"מספר תעודת זהות: {t_id}"
    restored, leftover = restore_content(body, home)
    assert "100000009" in restored and "⟦" not in restored and leftover == []


def test_catches_chr_constructed_token(tmp_path):
    """The exact field bug: a script builds ⟦מזהה_1⟧ from char codes. The literal
    token only exists at egress time — restore_content (run on the final string) catches it."""
    home = str(tmp_path)
    _seed_vault(home, [("100000009", "מזהה")])
    # what the model's script produced at runtime (chr(0x27E6)+'מזהה_1'+chr(0x27E7)):
    L, R = chr(0x27E6), chr(0x27E7)
    email_body = "שלום רב,\n\nמספר תעודת זהות: " + L + "מזהה_1" + R + "\n\nבברכה"
    restored, leftover = restore_content(email_body, home)
    assert "100000009" in restored and "⟦מזהה_1⟧" not in restored and leftover == []


def test_merges_all_vault_files(tmp_path):
    """Tokens minted across different vault files (sessions/restarts) all restore."""
    home = str(tmp_path)
    vdir = os.path.join(home, "cloak", "vaults"); os.makedirs(vdir)
    # two separate vault files (e.g. old per-session leftovers)
    for fn, real, tok in [("a.json", "100000009", "⟦מזהה_1⟧"), ("b.json", "100000034", "⟦מזהה_2⟧")]:
        with open(os.path.join(vdir, fn), "w", encoding="utf-8") as f:
            json.dump({"token_to_real": {tok: real}}, f, ensure_ascii=False)
    tm = load_token_map(home)
    assert tm["⟦מזהה_1⟧"] == "100000009" and tm["⟦מזהה_2⟧"] == "100000034"
    restored, _ = restore_content("ת\"ז ⟦מזהה_1⟧ וח.פ ⟦מזהה_2⟧", home)
    assert "100000009" in restored and "100000034" in restored


def test_leftover_reported_and_audited(tmp_path):
    home = str(tmp_path)
    _seed_vault(home, [("100000009", "מזהה")])
    restored, leftover = restore_content("ת\"ז ⟦מזהה_1⟧ וגם ⟦תז_9⟧", home)
    assert "100000009" in restored          # known token restored
    assert leftover == ["⟦תז_9⟧"]           # unknown token flagged, not silently passed


def test_restore_file_in_place(tmp_path):
    home = str(tmp_path)
    v = _seed_vault(home, [("050-1234567", "טלפון")])
    tok = v.tokenize("050-1234567", "טלפון")
    p = tmp_path / "out.txt"
    p.write_text(f"טלפון ליצירת קשר: {tok}", encoding="utf-8")
    leftover = restore_file(str(p), home)
    assert leftover == [] and "050-1234567" in p.read_text(encoding="utf-8") and "⟦" not in p.read_text(encoding="utf-8")


def test_no_vault_no_crash(tmp_path):
    # no vault dir at all → returns content unchanged, no exception
    restored, leftover = restore_content("ת\"ז ⟦מזהה_1⟧", str(tmp_path))
    assert restored == "ת\"ז ⟦מזהה_1⟧" and leftover == ["⟦מזהה_1⟧"]
