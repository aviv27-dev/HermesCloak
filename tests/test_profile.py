from hermescloak.instruction import TOKEN_INSTRUCTION
from hermescloak.profile import Profile

def test_instruction_mentions_verbatim_and_brackets():
    assert "⟦" in TOKEN_INSTRUCTION and "מילה במילה" in TOKEN_INSTRUCTION

def test_profile_from_yaml(tmp_path):
    p = tmp_path / "example.yaml"
    p.write_text(
        "profile: example\n"
        "languages: [he, en]\n"
        "fail_mode: open\n"
        "never_mask: [\"בית המשפט\"]\n"
        "token_instruction: true\n",
        encoding="utf-8",
    )
    prof = Profile.from_yaml(str(p))
    assert prof.name == "example"
    assert prof.languages == ["he", "en"]
    assert prof.fail_mode == "open"
    assert prof.never_mask == ["בית המשפט"]
    assert prof.token_instruction is True

def test_profile_defaults(tmp_path):
    p = tmp_path / "min.yaml"
    p.write_text("profile: x\n", encoding="utf-8")
    prof = Profile.from_yaml(str(p))
    assert prof.fail_mode == "open"          # default per spec §6
    assert prof.languages == ["he", "en"]    # default
    assert prof.never_mask == []
