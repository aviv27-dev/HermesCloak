"""Typed decisions from a System One model (TypeSafe's Jev through OpenRouter).

One door for every yes/no, pick-one or score question HermesCloak wants answered by a model that
returns TYPED, calibrated answers instead of text: ``decide(use_case, state, questions)``. Jev reads
the state and answers every question in parallel in one call (~0.3–0.6 s).

Contract — what may be sent:
  The state leaves the machine (OpenRouter → TypeSafe). Callers pass ONLY text that already went
  through the engine (tokens instead of values); this module does not mask anything itself.

Guarantees — the call never hurts the agent:
  * never raises: a bad input is ``{"ok": False, "reason": "invalid"}``;
  * no key (``OPENROUTER_API_KEY`` or ``HERMESCLOAK_JEV_KEY``) → ``reason: "no-key"``, nothing sent;
  * a timeout returns ``reason: "timeout"`` and the caller carries on with what it did before;
  * circuit breaker: ``breaker_fails`` consecutive failures → no calls for ``breaker_s`` (``"breaker"``);
  * concurrency cap: a call past ``concurrency`` in flight waits for a slot inside its own deadline,
    else ``reason: "busy"``;
  * ``question_version``: sha1 of a question's type, instructions and options (10 hex) travels
    with every answer, so answers recorded before and after a prompt edit can be told apart.

Questions: ``{name: {"type": "noul"|"choice"|"score", "instructions": str, "criteria": ...}}``
  noul   → criteria {"true": …, "false": …}; the answer carries ``noul`` (probability of true)
  choice → criteria {key: description}, 2–255 keys; the answer carries ``choice`` + ``confidence``
  score  → criteria [low, …, high]; the answer carries ``score`` + ``probabilities`` by index
Optional ``minConfidence`` on a choice marks a weaker answer ``low: True``.

Design after AGLO-OS's decide.js (the office's own module, 10.2026).
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import urllib.error
import urllib.request

JEV_URL = "https://openrouter.ai/api/alpha/decisions"
JEV_MODEL = "~typesafe/jev-latest"
TYPES = ("choice", "noul", "score")
DEFAULTS = dict(timeout_s=3.0, concurrency=2, breaker_fails=3, breaker_s=600.0, max_choice=255,
                max_questions=64)


def question_version(q: dict) -> str:
    """sha1(type + instructions + options) → 10 hex. Same question text = same hash, across restarts."""
    q = q or {}
    opts = q.get("criteria", q.get("options"))
    raw = json.dumps([q.get("type", ""), q.get("instructions", ""), opts], ensure_ascii=False,
                     sort_keys=True)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10]


def _wire(q: dict) -> dict:
    out = {"type": q["type"], "instructions": q["instructions"]}
    c = q.get("criteria", q.get("options"))
    if c is not None:
        out["criteria"] = c
    return out


def validate(questions) -> str | None:
    if not isinstance(questions, dict) or not questions:
        return "no questions"
    if len(questions) > DEFAULTS["max_questions"]:
        return f"more than {DEFAULTS['max_questions']} questions"
    for name, q in questions.items():
        if not isinstance(q, dict) or q.get("type") not in TYPES:
            return f"{name}: unknown type {q.get('type') if isinstance(q, dict) else q!r}"
        if not q.get("instructions"):
            return f"{name}: no instructions"
        c = q.get("criteria", q.get("options"))
        if q["type"] == "choice":
            k = len(c) if isinstance(c, dict) else 0
            if k < 2 or k > DEFAULTS["max_choice"]:
                return f"{name}: a choice needs 2–{DEFAULTS['max_choice']} options (got {k})"
        if q["type"] == "score" and not (isinstance(c, list) and len(c) >= 2):
            return f"{name}: a score needs a low→high list"
    return None


def _http_post(url: str, body: bytes, headers: dict, timeout: float):
    """(status, parsed-json-or-None). Raises on a transport error / timeout."""
    req = urllib.request.Request(url, data=body, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            status = resp.status
    except urllib.error.HTTPError as e:
        raw, status = e.read(), e.code
    try:
        return status, json.loads(raw.decode("utf-8"))
    except Exception:
        return status, None


class Decider:
    def __init__(self, *, url: str | None = None, model: str | None = None, env=None, post=_http_post,
                 now=time.monotonic, timeout_s: float = DEFAULTS["timeout_s"],
                 concurrency: int = DEFAULTS["concurrency"], breaker_fails: int = DEFAULTS["breaker_fails"],
                 breaker_s: float = DEFAULTS["breaker_s"]) -> None:
        self._env = env if env is not None else os.environ
        self._url, self._model = url, model
        self._post, self._now = post, now
        self._timeout, self._breaker_fails, self._breaker_s = timeout_s, breaker_fails, breaker_s
        self._slots = threading.BoundedSemaphore(max(1, int(concurrency)))
        self._concurrency = max(1, int(concurrency))
        self._lock = threading.Lock()
        self._fails = 0
        self._open_until = 0.0
        self._calls = self._errors = 0
        self._inflight = 0
        self._last_error: str | None = None
        self._last_ok_at: float | None = None

    # ---- configuration (env wins so a deployment can point at a proxy / another model) ----
    def url(self) -> str:
        return self._env.get("HERMESCLOAK_JEV_URL") or self._url or JEV_URL

    def model(self) -> str:
        return self._env.get("HERMESCLOAK_JEV_MODEL") or self._model or JEV_MODEL

    def key(self) -> str:
        return self._env.get("OPENROUTER_API_KEY") or self._env.get("HERMESCLOAK_JEV_KEY") or ""

    def configured(self) -> bool:
        return bool(self.key())

    # ---- the call ----
    def decide(self, use_case: str, state, questions: dict, timeout_s: float | None = None) -> dict:
        uc = str(use_case or "").strip()
        if not uc:
            return {"ok": False, "reason": "invalid", "error": "use_case required"}
        bad = validate(questions)
        if bad:
            return {"ok": False, "reason": "invalid", "error": bad}
        with self._lock:
            if self._open_until > self._now():
                return {"ok": False, "reason": "breaker", "retry_in_s": round(self._open_until - self._now(), 1)}
        key = self.key()
        if not key:
            return {"ok": False, "reason": "no-key"}
        qv = {n: question_version(q) for n, q in questions.items()}
        body = json.dumps({"model": self.model(), "state": state,
                           "questions": {n: _wire(q) for n, q in questions.items()}}, ensure_ascii=False)
        headers = {"Content-Type": "application/json", "Authorization": "Bearer " + key,
                   "X-Title": "HermesCloak decide"}
        tmo = max(0.05, float(timeout_s or self._timeout))
        deadline = self._now() + tmo
        if not self._slots.acquire(timeout=tmo):
            return {"ok": False, "reason": "busy"}
        t0 = self._now()
        with self._lock:
            self._inflight += 1
        try:
            left = max(0.05, deadline - self._now())
            status, j = self._post(self.url(), body.encode("utf-8"), headers, left)
            if status != 200:
                msg = ""
                if isinstance(j, dict) and j.get("error"):
                    err = j["error"]
                    msg = ": " + str(err.get("message") if isinstance(err, dict) else err)[:120]
                out = {"ok": False, "reason": "error", "error": f"HTTP {status}{msg}"}
            elif not isinstance(j, dict) or not isinstance(j.get("answers"), dict):
                out = {"ok": False, "reason": "bad-response", "error": "no answers"}
            else:
                answers = {}
                for n, q in questions.items():
                    a = j["answers"].get(n)
                    if not isinstance(a, dict):
                        answers[n] = None
                        continue
                    a = {**a, "qv": qv[n]}
                    mc = q.get("minConfidence")
                    try:
                        if mc is not None and float(a.get("confidence")) < float(mc):
                            a["low"] = True
                    except (TypeError, ValueError):
                        pass
                    answers[n] = a
                out = {"ok": True, "model": j.get("model") or self.model(), "answers": answers, "qv": qv}
        except Exception as e:  # noqa: BLE001 — transport errors, timeouts, bad JSON: all one shape
            timed_out = isinstance(e, TimeoutError) or "timed out" in str(e).lower() \
                or getattr(getattr(e, "reason", None), "__class__", type(None)).__name__ == "timeout"
            out = {"ok": False, "reason": "timeout" if timed_out else "error",
                   "error": "timeout" if timed_out else str(e)[:120]}
        finally:
            self._slots.release()
            with self._lock:
                self._inflight -= 1
        ms = int((self._now() - t0) * 1000)
        with self._lock:
            self._calls += 1
            if out["ok"]:
                self._fails = 0
                self._last_ok_at = self._now()
            else:
                self._errors += 1
                self._fails += 1
                self._last_error = out["reason"] + (": " + out["error"] if out.get("error") not in (None, out["reason"]) else "")
                if self._fails >= self._breaker_fails:
                    self._open_until = self._now() + self._breaker_s
                    self._fails = 0
        return {**out, "provider": "jev", "ms": ms}

    def health(self) -> dict:
        """Numbers and tags only — never a key, a state or an answer."""
        with self._lock:
            now = self._now()
            return {"provider": "jev", "url": self.url(), "model": self.model(), "has_key": self.configured(),
                    "inflight": self._inflight, "concurrency": self._concurrency, "calls": self._calls,
                    "errors": self._errors, "consecutive_fails": self._fails,
                    "breaker_open": self._open_until > now,
                    "breaker_s_left": round(max(0.0, self._open_until - now), 1),
                    "last_error": self._last_error, "last_ok_s_ago": None if self._last_ok_at is None
                    else round(now - self._last_ok_at, 1)}


_shared: Decider | None = None
_shared_lock = threading.Lock()


def shared() -> Decider:
    """One process-wide Decider, so the breaker and the concurrency cap are shared by every engine."""
    global _shared
    with _shared_lock:
        if _shared is None:
            _shared = Decider()
        return _shared


def decide(use_case: str, state, questions: dict, timeout_s: float | None = None) -> dict:
    return shared().decide(use_case, state, questions, timeout_s)


def health() -> dict:
    return shared().health()
