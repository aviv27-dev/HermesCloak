#!/usr/bin/env python3
"""End-to-end proof against a REAL hermes-agent: run one AIAgent turn (streaming) against a local
fake OpenAI-compatible "cloud" that records every request, then check that no real value reached
it and that the reply/stream/history came back restored. Uses a throwaway HERMES_HOME (MODE=enforce,
plugin enabled) — your real home is never touched. Run it with hermes' python, from anywhere:

  /path/to/hermes-venv/bin/python install/e2e_check.py --hermes-root /path/to/hermes-agent

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


def _server(log):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            self._json({"object": "list", "data": [{"id": "fake", "object": "model"}]})

        def _json(self, obj):
            b = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            log.append(body)
            user = json.dumps([m for m in body.get("messages", []) if m.get("role") != "system"],
                              ensure_ascii=False)
            toks = sorted(set(re.findall(r"⟦[^⟧_]+_\d+⟧", user)))
            reply = "Noted: " + ", ".join(toks) + "."        # echo the tokens the "cloud" saw
            if not body.get("stream"):
                return self._json({"id": "c", "object": "chat.completion", "created": 0, "model": "fake",
                                   "choices": [{"index": 0, "finish_reason": "stop",
                                                "message": {"role": "assistant", "content": reply}}],
                                   "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for i in range(0, len(reply), 3):                  # 3-char chunks split tokens mid-way
                ch = {"id": "c", "object": "chat.completion.chunk", "created": 0, "model": "fake",
                      "choices": [{"index": 0, "delta": {"content": reply[i:i + 3]}, "finish_reason": None}]}
                self.wfile.write(f"data: {json.dumps(ch, ensure_ascii=False)}\n\n".encode())
            end = {"id": "c", "object": "chat.completion.chunk", "created": 0, "model": "fake",
                   "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
            self.wfile.write(f"data: {json.dumps(end)}\n\ndata: [DONE]\n\n".encode())

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hermes-root", required=True)
    a = ap.parse_args()
    home = tempfile.mkdtemp(prefix="hermescloak-e2e-")
    os.makedirs(os.path.join(home, "cloak"))
    with open(os.path.join(home, "cloak", "MODE"), "w") as f:
        f.write("enforce")
    with open(os.path.join(home, "cloak", "gazetteer.txt"), "w", encoding="utf-8") as f:
        f.write(f"{PII['name']}\tלקוח\n")
    with open(os.path.join(home, "config.yaml"), "w") as f:
        f.write("plugins:\n  enabled: [hermescloak]\n")
    os.environ["HERMES_HOME"] = home
    sys.path.insert(0, os.path.abspath(a.hermes_root))
    os.chdir(a.hermes_root)

    log: list = []
    srv = _server(log)
    from run_agent import AIAgent
    deltas: list = []
    agent = AIAgent(base_url=f"http://127.0.0.1:{srv.server_address[1]}/v1", api_key="x", model="fake",
                    provider="custom", api_mode="chat_completions", quiet_mode=True, enabled_toolsets=[],
                    skip_context_files=True, skip_memory=True, skip_background_review=True,
                    stream_delta_callback=lambda t: deltas.append(t) if t else None, session_id="e2e")
    res = agent.run_conversation(PROMPT)
    final = res.get("final_response") or ""
    streamed = "".join(deltas)
    sent = json.dumps(log, ensure_ascii=False)
    checks = {
        "a request reached the fake cloud": bool(log),
        "no real value in any cloud-bound request": not any(v in sent for v in PII.values()),
        "tokens reached the cloud": "⟦" in sent,
        "final reply restored": all(v in final for v in PII.values()) and "⟦" not in final,
        "streamed reply restored": all(v in streamed for v in PII.values()) and "⟦" not in streamed,
    }
    audit = open(os.path.join(home, "cloak", "audit.log"), encoding="utf-8").read()
    checks["plugin_active, no seam_missing"] = "plugin_active" in audit and "seam_missing" not in audit
    for k, v in checks.items():
        print(f"  [{'OK ' if v else 'FAIL'}] {k}")
    srv.shutdown()
    ok = all(checks.values())
    print("--- " + ("PROTECTED ✓" if ok else "NOT PROTECTED") + f" (scratch home: {home}) ---")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
