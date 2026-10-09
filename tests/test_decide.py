"""hermescloak.decide against a fake OpenRouter: wire shape, key handling, and every guard
(timeout → ok False, breaker, concurrency cap, invalid input, bad response). No test reaches a
real service."""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from hermescloak import decide as D

Q = {
    "kind": {"type": "choice", "instructions": "איזה סוג?", "criteria": {"a": "א", "b": "ב", "none": "לא ברור"},
             "minConfidence": 0.5},
    "reply": {"type": "noul", "instructions": "צריך תשובה?", "criteria": {"true": "כן", "false": "לא"}},
    "urgency": {"type": "score", "instructions": "כמה דחוף?", "criteria": ["לא", "קצת", "מאוד"]},
}


class _Fake:
    def __init__(self, *, delay=0.0, status=200, answers=None, raw=None):
        self.calls, self.delay, self.status, self.answers, self.raw = [], delay, status, answers, raw
        self.inflight = self.max_inflight = 0
        self.lock = threading.Lock()
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                with fake.lock:
                    fake.inflight += 1
                    fake.max_inflight = max(fake.max_inflight, fake.inflight)
                    fake.calls.append({"path": self.path, "auth": self.headers.get("Authorization"),
                                       "title": self.headers.get("X-Title"), "body": json.loads(body)})
                time.sleep(fake.delay)
                b = fake.calls[-1]["body"]
                if fake.raw is not None:
                    out = fake.raw
                else:
                    ans = fake.answers or {
                        n: ({"type": "choice", "choice": list(q["criteria"])[0], "confidence": 0.4}
                            if q["type"] == "choice" else {"type": "noul", "noul": 0.9} if q["type"] == "noul"
                            else {"type": "score", "score": 1.2, "probabilities": {"0": .1, "1": .7, "2": .2}})
                        for n, q in b["questions"].items()}
                    out = json.dumps({"model": b["model"], "answers": ans}).encode()
                with fake.lock:
                    fake.inflight -= 1
                try:
                    self.send_response(fake.status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(out)))
                    self.end_headers()
                    self.wfile.write(out)
                except (BrokenPipeError, ConnectionResetError):
                    pass                                   # the client timed out first

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}/api/alpha/decisions"

    def close(self):
        self.srv.shutdown()


@pytest.fixture
def fake():
    f = _Fake()
    yield f
    f.close()


def _decider(fake, **kw):
    env = {"OPENROUTER_API_KEY": "k-test"}
    env.update(kw.pop("env", {}))
    return D.Decider(url=fake.url, env=env, **kw)


def test_wire_shape_key_and_answers(fake):
    d = _decider(fake)
    r = d.decide("mail:triage", {"text": "⟦לקוח_1⟧ שלח"}, Q)
    assert r["ok"] and r["provider"] == "jev" and r["model"] == D.JEV_MODEL
    c = fake.calls[0]
    assert c["auth"] == "Bearer k-test" and c["path"] == "/api/alpha/decisions"
    assert set(c["body"]) == {"model", "state", "questions"}
    assert c["body"]["questions"]["kind"] == {"type": "choice", "instructions": "איזה סוג?",
                                             "criteria": Q["kind"]["criteria"]}   # our own fields stay home
    assert r["answers"]["kind"]["low"] is True                                    # 0.4 < minConfidence 0.5
    assert r["answers"]["reply"]["noul"] == 0.9
    assert r["answers"]["kind"]["qv"] == r["qv"]["kind"] == D.question_version(Q["kind"])
    assert d.health()["backends"]["jev"]["calls"] == 1 and d.health()["has_key"] is True
    assert d.health()["backends"]["local"]["configured"] is False          # no url → never a fallback target


def test_question_version_is_stable_and_changes_with_the_prompt():
    assert D.question_version(Q["kind"]) == D.question_version(dict(Q["kind"]))
    assert D.question_version(Q["kind"]) != D.question_version({**Q["kind"], "instructions": "x"})
    assert len(D.question_version(Q["kind"])) == 10


def test_no_key_sends_nothing(fake):
    r = _decider(fake, env={"OPENROUTER_API_KEY": ""}).decide("x:y", {}, Q)
    assert r["ok"] is False and r["reason"] == "no-key" and fake.calls == []


def test_env_overrides_url_model_and_accepts_the_hermescloak_key_name(fake):
    d = D.Decider(env={"HERMESCLOAK_JEV_KEY": "k2", "HERMESCLOAK_JEV_URL": fake.url, "HERMESCLOAK_JEV_MODEL": "m2"})
    assert d.decide("x:y", {}, Q)["ok"] and fake.calls[0]["body"]["model"] == "m2"
    assert fake.calls[0]["auth"] == "Bearer k2"


@pytest.mark.parametrize("questions,msg", [
    ({}, "no questions"),
    ({"q": {"type": "text", "instructions": "x"}}, "unknown type"),
    ({"q": {"type": "noul"}}, "no instructions"),
    ({"q": {"type": "choice", "instructions": "x", "criteria": {"a": "א"}}}, "2–255 options"),
    ({"q": {"type": "score", "instructions": "x", "criteria": ["one"]}}, "low→high"),
])
def test_invalid_questions_never_reach_the_wire(fake, questions, msg):
    r = _decider(fake).decide("x:y", {}, questions)
    assert r["ok"] is False and r["reason"] == "invalid" and msg in r["error"] and fake.calls == []


def test_timeout_returns_ok_false_and_never_raises():
    f = _Fake(delay=0.6)
    try:
        r = _decider(f, timeout_s=0.15).decide("x:y", {}, Q)
        assert r["ok"] is False and r["reason"] == "timeout"
    finally:
        f.close()


def test_breaker_opens_after_consecutive_failures_and_recloses():
    f = _Fake(status=500, raw=b'{"error":{"message":"boom"}}')
    clock = [1000.0]
    try:
        d = _decider(f, breaker_fails=3, breaker_s=600, now=lambda: clock[0])
        for _ in range(3):
            assert d.decide("x:y", {}, Q)["reason"] == "error"
        assert d.decide("x:y", {}, Q)["reason"] == "breaker" and len(f.calls) == 3
        assert d.health()["backends"]["jev"]["breaker_open"] is True
        clock[0] += 601
        assert d.decide("x:y", {}, Q)["reason"] == "error" and len(f.calls) == 4   # one probe let through
    finally:
        f.close()


def test_concurrency_cap_waits_inside_the_deadline_else_busy():
    f = _Fake(delay=0.3)
    try:
        d = _decider(f, concurrency=2, timeout_s=0.12)
        out = []
        ts = [threading.Thread(target=lambda: out.append(d.decide("x:y", {}, Q))) for _ in range(3)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        reasons = sorted(r.get("reason", "ok") for r in out)
        assert "busy" in reasons and f.max_inflight <= 2
    finally:
        f.close()


def test_bad_response_body_is_a_typed_failure():
    f = _Fake(raw=b"not json at all")
    try:
        r = _decider(f).decide("x:y", {}, Q)
        assert r["ok"] is False and r["reason"] == "bad-response"
    finally:
        f.close()


def test_health_never_carries_the_key(fake):
    h = _decider(fake).health()
    assert "k-test" not in json.dumps(h) and h["has_key"] is True


# ---- the office's own decision model as a backend (AGLO-RAG train/decide_model.py serve)

class _FakeLocal(_Fake):
    """Speaks /v1/systemone: {model, keep_alive, state, questions} → {answers} (no model key)."""
    def __init__(self, **kw):
        super().__init__(**kw)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"


def _local_answers(b):
    return json.dumps({"answers": {n: ({"type": "noul", "noul": 0.2} if q["type"] == "noul"
                                      else {"type": "choice", "choice": "b", "probabilities": {"a": .3, "b": .6, "none": .1},
                                            "confidence": 0.6})
                                   for n, q in b["questions"].items()}}).encode()


def test_local_backend_wire_and_env_selection():
    f = _FakeLocal()
    try:
        d = D.Decider(env={"HERMESCLOAK_DECIDE_BACKEND": "local", "HERMESCLOAK_LOCAL_DECIDE_URL": f.url})
        assert d.backend() == "local" and d.configured() and not d.configured("jev")
        r = d.decide("residual:request", {"text": "x"}, Q)
        assert r["ok"] and r["backend"] == "local" and r["model"] == D.LOCAL_MODEL
        c = f.calls[0]
        assert c["path"] == "/v1/systemone" and c["auth"] is None
        assert c["body"]["model"] == D.LOCAL_MODEL and c["body"]["keep_alive"] == -1
        assert set(c["body"]) == {"model", "keep_alive", "state", "questions"}
    finally:
        f.close()


def test_local_refuses_choices_over_26_options_without_a_call():
    f = _FakeLocal()
    try:
        d = D.Decider(env={"HERMESCLOAK_LOCAL_DECIDE_URL": f.url}, backend="local", fallback=False)
        big = {"k": {"type": "choice", "instructions": "x", "criteria": {f"o{i}": str(i) for i in range(30)}}}
        r = d.decide("x:y", {}, big)
        assert r["reason"] == "invalid" and "2–26" in r["error"] and f.calls == []
    finally:
        f.close()


def test_primary_falls_back_to_the_other_backend_and_says_so():
    dead = _FakeLocal(); dead.close()
    jev = _Fake()
    try:
        d = D.Decider(env={"OPENROUTER_API_KEY": "k", "HERMESCLOAK_JEV_URL": jev.url,
                           "HERMESCLOAK_LOCAL_DECIDE_URL": dead.url, "HERMESCLOAK_DECIDE_BACKEND": "local"},
                      timeout_s=1.0)
        r = d.decide("x:y", {}, Q)
        assert r["ok"] and r["backend"] == "jev" and r["fell_back_from"] == "local"
        assert d.decide("x:y", {}, Q, fallback=False)["ok"] is False
    finally:
        jev.close()


def test_shadow_asks_the_other_backend_in_the_background_with_briefs_only():
    jev, loc = _Fake(), _FakeLocal()
    got = []
    done = threading.Event()
    try:
        d = D.Decider(env={"OPENROUTER_API_KEY": "k", "HERMESCLOAK_JEV_URL": jev.url,
                           "HERMESCLOAK_LOCAL_DECIDE_URL": loc.url})
        r = d.decide("residual:request", {"text": "⟦לקוח_1⟧"}, Q, shadow=True,
                     on_shadow=lambda rec: (got.append(rec), done.set()))
        assert r["ok"] and r["backend"] == "jev"
        assert done.wait(5)
        rec = got[0]
        assert rec["primary"] == "jev" and rec["shadow"] == "local" and rec["use_case"] == "residual:request"
        assert rec["primary_answers"]["reply"] == {"p": 0.9} and "c" in rec["primary_answers"]["kind"]
        assert rec["shadow_answers"] is not None and rec["shadow_ms"] is not None
        assert "⟦לקוח_1⟧" not in json.dumps(rec, ensure_ascii=False)        # no state in the record
        assert loc.calls and loc.calls[0]["body"]["questions"].keys() == Q.keys()
    finally:
        jev.close(); loc.close()


def test_calibration_temperature_softens_and_is_applied_per_use():
    a = {"q": {"type": "choice", "choice": "b", "probabilities": {"a": .1, "b": .9}, "confidence": .9},
         "n": {"type": "noul", "noul": 0.9}}
    c = D.calibrate(a, {"q": 2.0, "n": 2.0})
    assert 0.5 < c["q"]["probabilities"]["b"] < 0.9 and c["q"]["confidence"] == c["q"]["probabilities"]["b"]
    assert 0.5 < c["n"]["noul"] < 0.9 and a["n"]["noul"] == 0.9                 # input untouched
    assert D.calibrate(a, {"q": 1.0}) == a and D.calibrate(a, None) == a
    f = _Fake()
    try:
        d = D.Decider(env={"OPENROUTER_API_KEY": "k"}, url=f.url, calibration={"residual": {"reply": 3.0}})
        r = d.decide("residual:request", {}, Q)
        assert 0.5 < r["answers"]["reply"]["noul"] < 0.9                        # 0.9 softened
        assert d.decide("other:use", {}, Q)["answers"]["reply"]["noul"] == 0.9  # other use: raw
    finally:
        f.close()


def test_daily_cap_is_announced_once_then_quiet(fake):
    d = _decider(fake, daily_cap=2)
    assert d.decide("x:y", {}, Q)["ok"] and d.decide("x:y", {}, Q)["ok"]
    r3, r4 = d.decide("x:y", {}, Q), d.decide("x:y", {}, Q)
    assert r3["reason"] == "daily-cap" and r3["announce"] is True
    assert r4["reason"] == "daily-cap" and r4["announce"] is False
    assert len(fake.calls) == 2 and d.health()["today_calls"] == 2


def test_choice_helper_uses_the_probability_not_jevs_confidence():
    ans = {"k": {"type": "choice", "choice": "a", "confidence": 0.34, "probabilities": {"a": 0.56, "b": 0.44}}}
    assert D.choice(ans, "k") == ("a", 0.56)
    assert D.prob({"n": {"noul": 0.7}}, "n") == 0.7 and D.prob({}, "n") is None
    assert D.brief(ans) == {"k": {"c": "a", "p": 0.56}}


def test_low_flag_uses_the_chosen_options_probability(fake):
    # fake choice answers carry confidence 0.4 and probabilities {first: 0.4} → low under minConfidence 0.5
    r = _decider(fake).decide("x:y", {}, Q)
    assert r["answers"]["kind"]["low"] is True
