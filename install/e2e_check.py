#!/usr/bin/env python3
"""End-to-end proof against a REAL hermes-agent.

Runs real AIAgent conversations against a local fake "cloud" (OpenAI chat-completions and
Anthropic Messages, both STREAMING, with a full tool loop) that records every request, then
checks:
  * no real value ever reached the cloud — prompt, tool results, or replayed history;
  * the tool EXECUTED with the real values (a probe tool records its arguments);
  * the streamed reply and the final reply show real values;
  * the model's own turns are replayed byte-identically (prompt-cache / thinking-signature safe);
  * the plugin loaded with every interception point + self-test OK, no restore errors.

Uses a throwaway HERMES_HOME (MODE=enforce, plugin enabled) — your real home is never touched:

  /path/to/hermes-venv/bin/python install/e2e_check.py --hermes-root /path/to/hermes-agent
  ... --provider openai|anthropic|both   (default both; anthropic needs the `anthropic` package)

Exit 0 = protected on this hermes version.
"""
import argparse
import json
import os
import re
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PII = {"name": "דנה כהן", "email": "jane@firm.org", "phone": "050-1234567"}
PROMPT = f"Please contact our client {PII['name']} at {PII['email']} or {PII['phone']}."
TOKEN = re.compile(r"⟦[^⟧_]+_\d+⟧")
PROBE_CALLS: list = []


def _tokens(body) -> list:
    msgs = [m for m in body.get("messages", []) if m.get("role") != "system"]
    return sorted(set(TOKEN.findall(json.dumps(msgs, ensure_ascii=False))))


def _has_tool_result(body) -> bool:
    last = (body.get("messages") or [{}])[-1]
    if last.get("role") == "tool":
        return True
    c = last.get("content")
    return isinstance(c, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c)


def _plan(body):
    """The fake model: first call → text + a probe tool call using the tokens it saw (plus a
    phone-shaped number it invents, which must survive replay unchanged); after the tool
    result → a final text reply naming the client token."""
    toks = _tokens(body)
    if not _has_tool_result(body):
        text = "Noted: " + ", ".join(toks) + ". Backup line 050-7654321."
        args = {"to": next((t for t in toks if "מייל" in t), ""), "note": " ".join(toks)}
        return text, args
    client = next((t for t in toks if "לקוח" in t), "")
    return f"Done for {client}.", None


class _Fake(BaseHTTPRequestHandler):
    log: list = []
    replies: list = []

    def log_message(self, *a):
        pass

    def _send(self, obj, code=200):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _sse(self, events, named=False):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for ev in events:
            head = f"event: {ev['type']}\n" if named else ""
            self.wfile.write(f"{head}data: {json.dumps(ev, ensure_ascii=False)}\n\n".encode())
        if not named:
            self.wfile.write(b"data: [DONE]\n\n")

    def do_GET(self):
        self._send({"object": "list", "data": [{"id": "fake", "object": "model"}]})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if not any(m.get("role") == "user" for m in body.get("messages") or []):
            return self._send({"error": {"message": "fake: no conversation"}}, 400)   # capability probes
        self.log.append(body)
        text, args = _plan(body)
        self.replies.append((text, args))
        if self.path.endswith("/messages"):
            return self._anthropic(body, text, args)
        return self._openai(body, text, args)

    def _openai(self, body, text, args):
        tc = [{"index": 0, "id": "call_1", "type": "function",
               "function": {"name": "cloak_probe", "arguments": json.dumps(args, ensure_ascii=False)}}] if args else None
        if not body.get("stream"):
            msg = {"role": "assistant", "content": text}
            if tc:
                msg["tool_calls"] = [{k: v for k, v in tc[0].items() if k != "index"}]
            return self._send({"id": "c", "object": "chat.completion", "created": 0, "model": "fake",
                               "choices": [{"index": 0, "message": msg,
                                            "finish_reason": "tool_calls" if tc else "stop"}],
                               "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})
        base = {"id": "c", "object": "chat.completion.chunk", "created": 0, "model": "fake"}
        evs = [{**base, "choices": [{"index": 0, "delta": {"role": "assistant", "content": text[i:i + 3]},
                                     "finish_reason": None}]} for i in range(0, len(text), 3)]
        if tc:
            raw = tc[0]["function"]["arguments"]
            evs.append({**base, "choices": [{"index": 0, "delta": {"tool_calls": [
                {"index": 0, "id": "call_1", "type": "function",
                 "function": {"name": "cloak_probe", "arguments": ""}}]}, "finish_reason": None}]})
            evs += [{**base, "choices": [{"index": 0, "delta": {"tool_calls": [
                {"index": 0, "function": {"arguments": raw[i:i + 5]}}]}, "finish_reason": None}]}
                for i in range(0, len(raw), 5)]
        evs.append({**base, "choices": [{"index": 0, "delta": {},
                                         "finish_reason": "tool_calls" if tc else "stop"}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})
        self._sse(evs)

    def _anthropic(self, body, text, args):
        stop = "tool_use" if args else "end_turn"
        if not body.get("stream"):
            content = [{"type": "text", "text": text}]
            if args:
                content.append({"type": "tool_use", "id": "toolu_1", "name": "cloak_probe", "input": args})
            return self._send({"id": "m", "type": "message", "role": "assistant", "model": "fake",
                               "content": content, "stop_reason": stop, "stop_sequence": None,
                               "usage": {"input_tokens": 1, "output_tokens": 1}})
        evs = [{"type": "message_start", "message": {
            "id": "m", "type": "message", "role": "assistant", "model": "fake", "content": [],
            "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 1}}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}]
        evs += [{"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text[i:i + 3]}}
                for i in range(0, len(text), 3)]
        evs.append({"type": "content_block_stop", "index": 0})
        if args:
            raw = json.dumps(args, ensure_ascii=False)
            evs.append({"type": "content_block_start", "index": 1, "content_block": {
                "type": "tool_use", "id": "toolu_1", "name": "cloak_probe", "input": {}}})
            evs += [{"type": "content_block_delta", "index": 1,
                     "delta": {"type": "input_json_delta", "partial_json": raw[i:i + 5]}}
                    for i in range(0, len(raw), 5)]
            evs.append({"type": "content_block_stop", "index": 1})
        evs += [{"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None},
                 "usage": {"output_tokens": 5}}, {"type": "message_stop"}]
        self._sse(evs, named=True)


def _register_probe():
    from tools.registry import registry
    schema = {"name": "cloak_probe", "description": "Send a note to a contact.",
              "parameters": {"type": "object", "properties": {"to": {"type": "string"},
                                                               "note": {"type": "string"}}}}

    def handler(args, **_kw):
        PROBE_CALLS.append(dict(args))
        return json.dumps({"sent": True, "to": args.get("to"), "echo": args.get("note")}, ensure_ascii=False)

    registry.register(name="cloak_probe", toolset="cloak_probe", schema=schema, handler=handler)


def _run(provider, port):
    from run_agent import AIAgent
    _Fake.log.clear()
    _Fake.replies.clear()
    PROBE_CALLS.clear()
    deltas: list = []
    kw = dict(api_key="x", model="fake", quiet_mode=True, enabled_toolsets=["cloak_probe"],
              skip_context_files=True, skip_memory=True, skip_background_review=True,
              stream_delta_callback=lambda t: deltas.append(t) if t else None, session_id=f"e2e-{provider}")
    if provider == "anthropic":
        kw.update(base_url=f"http://127.0.0.1:{port}", provider="anthropic", api_mode="anthropic_messages")
    else:
        kw.update(base_url=f"http://127.0.0.1:{port}/v1", provider="custom", api_mode="chat_completions")
    agent = AIAgent(**kw)
    res = agent.run_conversation(PROMPT)
    final = res.get("final_response") or ""
    streamed = "".join(deltas)
    sent = json.dumps(_Fake.log, ensure_ascii=False)
    first_text, first_args = _Fake.replies[0] if _Fake.replies else ("", None)
    # in the tool loop, request #2 carries the model's first turn back: must be byte-identical
    replay_ok = False
    if len(_Fake.log) >= 2:
        for m in _Fake.log[1].get("messages", []):
            if m.get("role") != "assistant":
                continue
            c = m.get("content")
            if provider == "anthropic" and isinstance(c, list):
                texts = [b.get("text") for b in c if b.get("type") == "text"]
                inputs = [b.get("input") for b in c if b.get("type") == "tool_use"]
                replay_ok = first_text in texts and first_args in inputs
            elif isinstance(c, str):
                calls = [json.loads(t["function"]["arguments"]) for t in m.get("tool_calls") or []]
                replay_ok = c == first_text and first_args in calls
    probe = PROBE_CALLS[0] if PROBE_CALLS else {}
    return {
        "cloud received requests (tool loop ran)": len(_Fake.log) >= 2,
        "no real value in ANY cloud-bound request": not any(v in sent for v in PII.values()),
        "tokens reached the cloud": "⟦" in sent,
        "tool executed with REAL values": probe.get("to") == PII["email"] and PII["phone"] in probe.get("note", ""),
        "final reply restored": PII["name"] in final and "⟦" not in final,
        "streamed reply restored": PII["email"] in streamed and "⟦" not in streamed,
        "model turn replayed byte-identical": replay_ok,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hermes-root", required=True)
    ap.add_argument("--provider", choices=["openai", "anthropic", "both"], default="both")
    a = ap.parse_args()
    home = tempfile.mkdtemp(prefix="hermescloak-e2e-")
    os.makedirs(os.path.join(home, "cloak"))
    with open(os.path.join(home, "cloak", "MODE"), "w") as f:
        f.write("enforce")
    with open(os.path.join(home, "cloak", "gazetteer.txt"), "w", encoding="utf-8") as f:
        f.write(f"{PII['name']}\tלקוח\n")
    with open(os.path.join(home, "config.yaml"), "w") as f:
        # tool_search off: hermes would otherwise defer the probe tool behind its search bridge
        f.write("plugins:\n  enabled: [hermescloak]\ntools:\n  tool_search:\n    enabled: off\n")
    os.environ["HERMES_HOME"] = home
    os.environ.setdefault("ANTHROPIC_API_KEY", "x")
    sys.path.insert(0, os.path.abspath(a.hermes_root))
    os.chdir(a.hermes_root)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    _register_probe()

    providers = ["openai", "anthropic"] if a.provider == "both" else [a.provider]
    ok, ran = True, False
    for p in providers:
        if p == "anthropic":
            try:
                import anthropic  # noqa: F401  # hermes pins anthropic==0.87.0; a newer SDK (httpx2) breaks its adapter
            except ImportError:
                print("  [SKIP] anthropic: `anthropic` package not installed in this python")
                continue
        print(f"--- {p} ---")
        try:
            checks = _run(p, srv.server_address[1])
        except Exception as exc:  # noqa: BLE001
            checks = {f"run completed ({type(exc).__name__}: {exc})": False}
        for k, v in checks.items():
            print(f"  [{'OK ' if v else 'FAIL'}] {k}")
        ok &= all(checks.values())
        ran = True
    if not ran:
        print("--- nothing ran (every provider skipped) ---")
        return 2
    audit = open(os.path.join(home, "cloak", "audit.log"), encoding="utf-8").read()
    tail = {
        "plugin_active, no seam_missing": "plugin_active" in audit and "seam_missing" not in audit,
        "self-test ok": '\\"self_test\\": \\"ok\\"' in audit,
        "no restore errors / leftovers / unfiltered sends":
            not any(k in audit for k in ("restore_error", "leftover_token", "unfiltered_sent")),
    }
    print("--- audit ---")
    for k, v in tail.items():
        print(f"  [{'OK ' if v else 'FAIL'}] {k}")
    ok &= all(tail.values())
    srv.shutdown()
    print("--- " + ("PROTECTED ✓" if ok else "NOT PROTECTED") + f" (scratch home: {home}) ---")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
