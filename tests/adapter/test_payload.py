import json

from hermescloak.adapter import payload as p

UP = str.upper  # a visible stand-in for "tokenize": every transformed string is upper-cased


def test_chat_completions_parts_tool_args_and_tool_results():
    req = {
        "model": "m",
        "tools": [{"type": "function", "function": {"name": "send", "description": "mail x"}}],
        "messages": [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": [{"type": "text", "text": "hi"},
                                         {"type": "image_url", "image_url": {"url": "data:abc"}}]},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "send", "arguments": '{"to": "bob"}'}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "sent"},
        ],
    }
    out = p.transform_request(req, UP)
    m = out["messages"]
    assert m[0]["content"] == "SYS"
    assert m[1]["content"][0]["text"] == "HI"
    assert m[1]["content"][1] == req["messages"][1]["content"][1]          # image untouched
    assert json.loads(m[2]["tool_calls"][0]["function"]["arguments"]) == {"to": "BOB"}
    assert m[2]["tool_calls"][0]["function"]["name"] == "send"             # ids/names untouched
    assert m[2]["tool_calls"][0]["id"] == "c1"
    assert m[3]["content"] == "SENT" and m[3]["tool_call_id"] == "c1"
    assert out["tools"] is req["tools"]                                     # schemas untouched
    assert req["messages"][0]["content"] == "sys"                          # input not mutated


def test_anthropic_blocks_skip_signed_thinking():
    req = {"system": [{"type": "text", "text": "s", "cache_control": {"type": "ephemeral"}}],
           "messages": [
               {"role": "assistant", "content": [
                   {"type": "thinking", "thinking": "secret", "signature": "sig"},
                   {"type": "tool_use", "id": "t", "name": "n", "input": {"q": "x", "n": {"deep": "y"}}}]},
               {"role": "user", "content": [
                   {"type": "tool_result", "tool_use_id": "t", "content": [{"type": "text", "text": "r"}]}]}]}
    out = p.transform_request(req, UP)
    assert out["system"][0]["text"] == "S" and out["system"][0]["cache_control"] == {"type": "ephemeral"}
    blocks = out["messages"][0]["content"]
    assert blocks[0] == {"type": "thinking", "thinking": "secret", "signature": "sig"}
    assert blocks[1]["input"] == {"q": "X", "n": {"deep": "Y"}} and blocks[1]["name"] == "n"
    assert out["messages"][1]["content"][0]["content"][0]["text"] == "R"


def test_responses_api_items_and_instructions():
    req = {"instructions": "be", "input": [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "a"}]},
        {"type": "function_call", "call_id": "x", "name": "f", "arguments": '{"p": "b"}'},
        {"type": "function_call_output", "call_id": "x", "output": "c"},
        {"type": "reasoning", "encrypted_content": "zzz", "summary": [{"type": "summary_text", "text": "s"}]}]}
    out = p.transform_request(req, UP)
    assert out["instructions"] == "BE"
    assert out["input"][0]["content"][0]["text"] == "A"
    assert json.loads(out["input"][1]["arguments"]) == {"p": "B"} and out["input"][1]["call_id"] == "x"
    assert out["input"][2]["output"] == "C"
    assert out["input"][3] is req["input"][3]                               # reasoning item verbatim


def test_bedrock_converse_shapes():
    req = {"system": [{"text": "s"}], "messages": [{"role": "assistant", "content": [
        {"toolUse": {"toolUseId": "u", "name": "n", "input": {"k": "v"}}},
        {"toolResult": {"toolUseId": "u", "content": [{"text": "t"}, {"json": {"a": "b"}}]}}]}]}
    out = p.transform_request(req, UP)
    c = out["messages"][0]["content"]
    assert out["system"] == [{"text": "S"}]
    assert c[0]["toolUse"] == {"toolUseId": "u", "name": "n", "input": {"k": "V"}}
    assert c[1]["toolResult"]["content"] == [{"text": "T"}, {"json": {"a": "B"}}]


def test_unchanged_arguments_keep_their_exact_bytes():
    raw = '{"to":   "bob"}'                     # odd spacing must survive a no-op (prompt cache)
    assert p.transform_json_string(raw, lambda s: s) is raw


def test_inject_instruction_per_wire_format():
    ins = "INS"
    chat = p.inject_instruction({"messages": [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]}, ins)
    assert chat["messages"][0]["content"] == "s\n\nINS"                    # appended, cache prefix kept
    assert p.inject_instruction(chat, ins)["messages"][0]["content"] == "s\n\nINS"   # idempotent
    bare = p.inject_instruction({"messages": [{"role": "user", "content": "u"}]}, ins)
    assert bare["messages"][0] == {"role": "system", "content": "INS"}
    anth = p.inject_instruction({"messages": [{"role": "user", "content": "u"}]}, ins, "anthropic_messages")
    assert anth["system"] == "INS" and anth["messages"] == [{"role": "user", "content": "u"}]
    assert p.inject_instruction({"instructions": "i", "input": []}, ins)["instructions"] == "i\n\nINS"
    bed = p.inject_instruction({"system": [{"text": "s"}], "messages": []}, ins)
    assert bed["system"][-1] == {"text": "INS"}


def test_unescape_tokens():
    assert p.unescape_tokens(r'x ⟦מייל_1⟧ y') == "x ⟦מייל_1⟧ y"
    assert p.unescape_tokens("plain") == "plain"
