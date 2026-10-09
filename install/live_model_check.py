#!/usr/bin/env python3
"""Live check against a REAL model (any OpenAI-compatible endpoint; default: Ollama Cloud).

Runs real hermes AIAgent conversations with HermesCloak enabled in a throwaway HERMES_HOME,
through a local RECORDING proxy that forwards to the real provider — so the check sees the
exact bytes that left the machine. Synthetic data only.

  HC_LIVE_BASE_URL   default https://ollama.com/v1
  HC_LIVE_API_KEY    default $OLLAMA_API_KEY
  HC_LIVE_MODEL      default gpt-oss:120b

  <hermes-venv>/bin/python install/live_model_check.py --hermes-root /path/to/hermes-agent

HARD checks (the privacy guarantee — must pass): no synthetic real value in ANY request that
left the machine in enforce mode; plugin active + self-test; no restore errors / unfiltered sends.
SOFT checks (how well THIS model handles tokens — informational): real values came back in the
reply, the tool received real values, replay used, leftover count.
Exit 0 = all hard checks passed.
"""
import argparse
import json
import os
import re
import ssl
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PII = {"name": "ישראל ישראלי", "id": "000000018", "phone": "050-0000000",
       "email": "test.client@example.org"}
SECRET = "sk-proj-TESTTESTTESTTESTTESTTEST"
REAL_VALUES = list(PII.values()) + [SECRET]
SENT: list = []
PROBE_CALLS: list = []


def _ssl_ctx():
    cafile = os.environ.get("SSL_CERT_FILE") or os.environ.get("REQUESTS_CA_BUNDLE")
    return ssl.create_default_context(cafile=cafile) if cafile else ssl.create_default_context()


def _recorder(upstream: str, api_key: str):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler(),   # honours HTTPS_PROXY
                                         urllib.request.HTTPSHandler(context=_ssl_ctx()))

    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def handle(self):
            try:
                super().handle()
            except (ConnectionResetError, BrokenPipeError):
                pass                       # the client closed a keep-alive connection

        def _forward(self, method, body=None):
            path = self.path[len("/v1"):] if self.path.startswith("/v1") else self.path
            req = urllib.request.Request(upstream.rstrip("/") + path, data=body, method=method)
            req.add_header("Authorization", f"Bearer {api_key}")
            req.add_header("Content-Type", self.headers.get("Content-Type", "application/json"))
            req.add_header("Accept", self.headers.get("Accept", "*/*"))
            try:
                resp = opener.open(req, timeout=300)
                status, headers = resp.status, resp.headers
            except urllib.error.HTTPError as e:
                resp, status, headers = e, e.code, e.headers
            self.send_response(status)
            ctype = headers.get("Content-Type", "application/json")
            self.send_header("Content-Type", ctype)
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            while True:
                chunk = resp.read1(8192) if hasattr(resp, "read1") else resp.read(8192)
                if not chunk:
                    break
                self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")

        def do_GET(self):
            self._forward("GET")

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            try:
                SENT.append(json.loads(body))
            except ValueError:
                SENT.append({"_raw": body.decode("utf-8", "replace")})
            self._forward("POST", body)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _register_probe():
    from tools.registry import registry
    schema = {"name": "send_note", "description": "Send a short note to a contact (email address).",
              "parameters": {"type": "object", "required": ["to", "body"],
                             "properties": {"to": {"type": "string", "description": "email address"},
                                            "body": {"type": "string"}}}}

    def handler(args, **_kw):
        PROBE_CALLS.append(dict(args))
        return json.dumps({"sent": True}, ensure_ascii=False)

    registry.register(name="send_note", toolset="cloak_probe", schema=schema, handler=handler)


def _agent(port, model, session, deltas):
    from run_agent import AIAgent
    return AIAgent(base_url=f"http://127.0.0.1:{port}/v1", api_key="via-recorder", model=model,
                   provider="custom", api_mode="chat_completions", quiet_mode=True,
                   enabled_toolsets=["cloak_probe"], skip_context_files=True, skip_memory=True,
                   skip_background_review=True, session_id=session,
                   stream_delta_callback=lambda t: deltas.append(t) if t else None)


def _leaks(requests) -> list:
    blob = json.dumps(requests, ensure_ascii=False)
    return [v for v in REAL_VALUES if v in blob]


def _audit(home):
    try:
        return [json.loads(x) for x in open(os.path.join(home, "cloak", "audit.log"), encoding="utf-8")]
    except OSError:
        return []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hermes-root", required=True)
    a = ap.parse_args()
    upstream = os.environ.get("HC_LIVE_BASE_URL", "https://ollama.com/v1")
    key = os.environ.get("HC_LIVE_API_KEY") or os.environ.get("OLLAMA_API_KEY", "")
    model = os.environ.get("HC_LIVE_MODEL", "gpt-oss:120b")
    if not key:
        print("set OLLAMA_API_KEY (or HC_LIVE_API_KEY) — as an environment variable, never in a chat")
        return 2

    home = tempfile.mkdtemp(prefix="hermescloak-live-")
    cloak = os.path.join(home, "cloak")
    os.makedirs(cloak)
    with open(os.path.join(cloak, "gazetteer.txt"), "w", encoding="utf-8") as f:
        f.write(f"{PII['name']}\tלקוח\n")
    with open(os.path.join(home, "config.yaml"), "w") as f:
        f.write("plugins:\n  enabled: [hermescloak]\ntools:\n  tool_search:\n    enabled: off\n")
    os.environ["HERMES_HOME"] = home
    sys.path.insert(0, os.path.abspath(a.hermes_root))
    os.chdir(a.hermes_root)
    srv = _recorder(upstream, key)
    port = srv.server_address[1]
    _register_probe()
    mode = lambda m: open(os.path.join(cloak, "MODE"), "w").write(m)  # noqa: E731
    hard, soft = {}, {}
    pii_line = (f"Client {PII['name']}, ID {PII['id']}, phone {PII['phone']}, "
                f"email {PII['email']}.")

    # 1. shadow: nothing changes (originals go out), detection is audited
    mode("shadow")
    SENT.clear()
    d: list = []
    _agent(port, model, "live-shadow", d).run_conversation(f"Reply with just 'ok'. {pii_line}")
    shadow_sent = list(SENT)
    hard["shadow: originals sent unchanged (zero behaviour change)"] = bool(_leaks(shadow_sent))
    hard["shadow: detection audited"] = any(e["kind"] == "shadow_detect" for e in _audit(home))

    # 2..5 enforce
    mode("enforce")
    SENT.clear()
    d = []
    ag = _agent(port, model, "live-enforce", d)
    r1 = ag.run_conversation(f"{pii_line} Repeat the client's email address and phone number "
                             "exactly as given, nothing else.")
    final1 = r1.get("final_response") or ""
    soft["reply shows the real email/phone (model kept the tokens)"] = (
        PII["email"] in final1 and PII["phone"] in final1)
    soft["streamed text restored"] = "⟦" not in "".join(d) and bool("".join(d).strip())
    r2 = ag.run_conversation("Now use the send_note tool to send the client a note at their email "
                             "address containing their phone number.",
                             conversation_history=r1.get("messages"))
    probe = PROBE_CALLS[-1] if PROBE_CALLS else {}
    soft["tool called"] = bool(PROBE_CALLS)
    soft["tool received the REAL email"] = probe.get("to") == PII["email"]
    r3 = ag.run_conversation(f"Remember this API key for later: {SECRET}. Reply 'stored'.",
                             conversation_history=r2.get("messages"))
    ag.run_conversation("What was the client's name? One line.",
                        conversation_history=r3.get("messages"))
    enforce_sent = [b for b in SENT if b.get("messages")]
    leaks = _leaks(enforce_sent)
    hard["enforce: requests reached the real model"] = len(enforce_sent) >= 3
    hard["enforce: NO synthetic real value in any outbound request"] = not leaks
    hard["enforce: tokens were sent instead"] = "⟦" in json.dumps(enforce_sent, ensure_ascii=False)

    audit = _audit(home)
    kinds = [e["kind"] for e in audit]
    details = [json.loads(e.get("detail") or "{}") if (e.get("detail") or "").startswith("{") else {}
               for e in audit]
    active = [x for k, x in zip(kinds, details) if k == "plugin_active"]
    hard["plugin active, every point ok, self-test ok"] = bool(active) and all(
        str(v).startswith("ok") for v in active[-1].get("patches", {}).values())
    hard["no restore_error / unfiltered_sent / seam_missing"] = not any(
        k in kinds for k in ("restore_error", "unfiltered_sent", "seam_missing"))
    soft["replay used on later turns"] = any(x.get("replayed") for k, x in zip(kinds, details)
                                             if k == "enforce_send")
    left = kinds.count("leftover_token")
    soft[f"no leftover tokens (found {left})"] = left == 0

    print(f"--- live model: {model} @ {upstream} ---")
    for k, v in hard.items():
        print(f"  [{'OK ' if v else 'FAIL'}] {k}")
    if leaks:
        print(f"         leaked: {leaks}")
    print("--- model behaviour (informational) ---")
    for k, v in soft.items():
        print(f"  [{'ok ' if v else 'warn'}] {k}")
    print("--- audit kinds ---")
    print("  " + ", ".join(f"{k}×{kinds.count(k)}" for k in sorted(set(kinds))))
    srv.shutdown()
    ok = all(hard.values())
    print("--- " + ("PROTECTED ✓" if ok else "NOT PROTECTED ✗") + f" (scratch home: {home}) ---")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
