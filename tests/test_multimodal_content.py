"""Masking must cover multimodal (list) content, not just str.

hermes builds list-of-blocks content on image/attachment turns
(agent/turn_context.py, agent/image_routing.py). Masking only str meant every such
turn went to the cloud in cleartext, and the leak counter — which joined str content
only — reported a clean 0 for exactly those payloads.
"""
import json

from hermescloak.engine import Engine
from hermescloak.profile import Profile

MAIL = "dana@example.org"
PHONE = "050-1234567"
TEXT = f"Email {MAIL}, call {PHONE}"


def _engine():
    return Engine(Profile(name="test"))


def test_str_content_is_masked():
    # NOTE: sanitize_outbound prepends a token-instruction system message when the
    # vault is non-empty, so the message under test is the LAST one, not the first.
    out = _engine().sanitize_outbound([{"role": "user", "content": TEXT}])
    assert MAIL not in out[-1]["content"]
    assert PHONE not in out[-1]["content"]


def test_list_content_is_masked():
    msg = {"role": "user", "content": [{"type": "text", "text": TEXT}]}
    out = _engine().sanitize_outbound([msg])
    blob = json.dumps(out, ensure_ascii=False)
    assert MAIL not in blob
    assert PHONE not in blob
    assert "⟦" in blob


def test_nested_tool_result_content_is_masked():
    msg = {"role": "user", "content": [
        {"type": "tool_result", "content": [{"type": "text", "text": TEXT}]}]}
    out = _engine().sanitize_outbound([msg])
    blob = json.dumps(out, ensure_ascii=False)
    assert MAIL not in blob
    assert PHONE not in blob


def test_image_blocks_are_left_intact():
    """Pixels can't be masked; the block must survive byte-identical."""
    img = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}
    msg = {"role": "user", "content": [img, {"type": "text", "text": TEXT}]}
    out = _engine().sanitize_outbound([msg])
    assert out[-1]["content"][0] == img
    assert MAIL not in out[-1]["content"][1]["text"]


def test_canonical_conversation_is_not_mutated():
    msg = {"role": "user", "content": [{"type": "text", "text": TEXT}]}
    _engine().sanitize_outbound([msg])
    assert msg["content"][0]["text"] == TEXT


def test_leak_counter_sees_list_content():
    """The proof-blob must cover every shape, or it under-reports its own blind spot."""
    eng = _engine()
    msg = {"role": "user", "content": [{"type": "text", "text": TEXT}]}
    eng.sanitize_outbound([msg])
    # An UNMASKED payload of the same shape must be counted as leaking.
    raw = json.dumps([msg], ensure_ascii=False)
    assert eng.vault.count_present(raw) == 2       # mail + phone both still present
