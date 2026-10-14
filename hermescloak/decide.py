"""Typed decisions from a System One model — Jev (TypeSafe via OpenRouter) or the office's own
locally served Hebrew decision model — behind one door: ``decide(use_case, state, questions)``.

A System One model never writes text: it reads a state and answers typed questions (yes/no
probability, pick-one with a probability per option, a score) in a few hundred ms. HermesCloak uses
it for second opinions on text that has ALREADY been masked.

Backends (``HERMESCLOAK_DECIDE_BACKEND`` / profile ``decide_backend``):
  jev    OpenRouter ``POST /api/alpha/decisions``, key ``OPENROUTER_API_KEY`` (or ``HERMESCLOAK_JEV_KEY``).
         A second vendor sees the masked text. ~0.3–0.6 s.
  local  a server speaking the same shape at ``HERMESCLOAK_LOCAL_DECIDE_URL`` + ``/v1/systemone``
         (default http://127.0.0.1:11600 — the office model behind an SSH tunnel). Nothing leaves
         the office network. Choice questions limited to 2–26 options there.
  A primary that gives no answer falls back to the other backend when ``fallback`` is on.

Guarantees — the call never hurts the agent:
  * never raises: a bad input is ``{"ok": False, "reason": "invalid"}``;
  * no key / no url → ``reason: "no-key"``, nothing sent;
  * a timeout returns ``reason: "timeout"`` and the caller carries on with what it did before;
  * circuit breaker per backend: 3 consecutive failures → 10 min off (``"breaker"``);
  * concurrency cap: a call past ``concurrency`` in flight waits inside its own deadline, else ``"busy"``;
  * daily cap (``HERMESCLOAK_DECIDE_DAILY_CAP``, default 20000 calls): ``"daily-cap"`` — reported,
    not silent: a silent cap once turned 9,000 checks into no verdicts unnoticed;
  * ``question_version``: sha1 of a question's type, instructions and options (10 hex) travels
    with every answer, so answers recorded before and after a prompt edit can be told apart.

Calibration: a softmax over summed log-probs does not make "0.75" mean "right 75% of the time".
``calibration`` = ``{use_case: {question: T}}`` applies ``p_i ∝ p_i^(1/T)`` (T>1 softens, T<1
sharpens), fitted per use on a labelled set (``HERMESCLOAK_DECIDE_CALIBRATION`` = a JSON file).

Shadow: with ``shadow=True`` and both backends configured, the OTHER backend is asked in the
background and ``on_shadow(record)`` receives both answers (briefs: probabilities only). The primary
answer is the one returned. This is how a new model earns its place before it is switched on.

Questions: ``{name: {"type": "noul"|"choice"|"score", "instructions": str, "criteria": ...}}``
  noul   → criteria {"true": …, "false": …}; the answer carries ``noul`` (probability of true)
  choice → criteria {key: description}; the answer carries ``choice`` + ``probabilities``
  score  → criteria [low, …, high]; the answer carries ``score`` + ``probabilities`` by index
Thresholds compare against ``probabilities[choice]`` (``choice()`` below), never against Jev's
``confidence``: that field is a separate margin measure and reads lower (0.34 for a 0.56 choice).

Design after AGLO-OS decide.js and AGLO-RAG jev.py / train/decide_model.py (the office's own
modules, 10.2026).
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import urllib.error
import urllib.request
from datetime import date

JEV_URL = "https://openrouter.ai/api/alpha/decisions"
JEV_MODEL = "~typesafe/jev-latest"
LOCAL_URL = "http://127.0.0.1:11600"
LOCAL_MODEL = "rag-hebrew-decide"
LOCAL_MAX_CHOICE = 26
TYPES = ("choice", "noul", "score")
BACKENDS = ("jev", "local")
DEFAULTS = dict(timeout_s=3.0, concurrency=2, breaker_fails=3, breaker_s=600.0, max_choice=255,
                max_questions=64, daily_cap=20000)


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


def validate(questions, max_choice: int = DEFAULTS["max_choice"]) -> str | None:
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
            if k < 2 or k > max_choice:
                return f"{name}: a choice needs 2–{max_choice} options (got {k})"
        if q["type"] == "score" and not (isinstance(c, list) and len(c) >= 2):
            return f"{name}: a score needs a low→high list"
    return None


def calibrate(answers: dict, temps: dict | None) -> dict:
    """Apply per-question temperatures: p_i ∝ p_i^(1/T). Returns a new dict; T missing or 1.0 → as is."""
    if not temps:
        return answers
    out = {}
    for name, a in (answers or {}).items():
        T = temps.get(name)
        if not isinstance(a, dict) or not T or T == 1.0:
            out[name] = a
            continue
        a = dict(a)
        if "noul" in a:
            t = max(float(a["noul"]), 1e-12) ** (1 / T)
            f = max(1 - float(a["noul"]), 1e-12) ** (1 / T)
            a["noul"] = t / (t + f)
        elif isinstance(a.get("probabilities"), dict) and a["probabilities"]:
            w = {k: max(float(v), 1e-12) ** (1 / T) for k, v in a["probabilities"].items()}
            z = sum(w.values())
            a["probabilities"] = {k: v / z for k, v in w.items()}
            if a.get("choice") in a["probabilities"]:
                a["confidence"] = a["probabilities"][a["choice"]]
        out[name] = a
    return out


def choice(answers: dict | None, name: str) -> tuple[str | None, float]:
    """(chosen key, probability OF THAT KEY) — what a threshold must compare against."""
    a = (answers or {}).get(name) or {}
    c = a.get("choice")
    probs = a.get("probabilities") if isinstance(a.get("probabilities"), dict) else {}
    p = probs.get(c, a.get("confidence"))
    try:
        return c, float(p or 0.0)
    except (TypeError, ValueError):
        return c, 0.0


def prob(answers: dict | None, name: str) -> float | None:
    """P(true) of a noul question, or None."""
    a = (answers or {}).get(name) or {}
    try:
        return None if a.get("noul") is None else float(a["noul"])
    except (TypeError, ValueError):
        return None


def brief(answers: dict | None) -> dict | None:
    """Probabilities only — safe to log."""
    if not answers:
        return None
    out = {}
    for k, a in answers.items():
        if not isinstance(a, dict):
            out[k] = None
        elif "noul" in a:
            out[k] = {"p": round(float(a["noul"]), 4)}
        else:
            c, p = choice(answers, k)
            out[k] = {"c": c, "p": round(p, 4)}
    return out


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


class _Breaker:
    def __init__(self) -> None:
        self.fails = 0
        self.open_until = 0.0
        self.calls = self.errors = 0
        self.last_error: str | None = None
        self.last_ok_at: float | None = None


class Decider:
    def __init__(self, *, url: str | None = None, model: str | None = None, local_url: str | None = None,
                 local_model: str | None = None, backend: str | None = None, fallback: bool = True,
                 calibration: dict | None = None, env=None, post=_http_post, now=time.monotonic,
                 timeout_s: float = DEFAULTS["timeout_s"], concurrency: int = DEFAULTS["concurrency"],
                 breaker_fails: int = DEFAULTS["breaker_fails"], breaker_s: float = DEFAULTS["breaker_s"],
                 daily_cap: int | None = None, today=date.today) -> None:
        self._env = env if env is not None else os.environ
        self._url, self._model, self._local_url, self._local_model = url, model, local_url, local_model
        self._backend, self._fallback = backend, fallback
        self._calibration = calibration
        self._post, self._now, self._today = post, now, today
        self._timeout, self._breaker_fails, self._breaker_s = timeout_s, breaker_fails, breaker_s
        self._concurrency = max(1, int(concurrency))
        self._slots = threading.BoundedSemaphore(self._concurrency)
        self._lock = threading.Lock()
        self._br = {b: _Breaker() for b in BACKENDS}
        self._inflight = 0
        self._daily_cap = daily_cap
        self._day = {"d": None, "n": 0, "announced": False}

    # ---- configuration (env wins so a deployment can point at a proxy / another model) ----
    def url(self) -> str:
        return self._env.get("HERMESCLOAK_JEV_URL") or self._url or JEV_URL

    def model(self) -> str:
        return self._env.get("HERMESCLOAK_JEV_MODEL") or self._model or JEV_MODEL

    def key(self) -> str:
        return self._env.get("OPENROUTER_API_KEY") or self._env.get("HERMESCLOAK_JEV_KEY") or ""

    def local_url(self) -> str:
        """Set explicitly (env / ctor), or the default tunnel address only when local IS the chosen
        backend — so a Jev-only deployment never knocks on 127.0.0.1:11600 as a fallback."""
        u = self._env.get("HERMESCLOAK_LOCAL_DECIDE_URL") or self._local_url
        if not u and self._raw_backend() == "local":
            u = LOCAL_URL
        return (u or "").rstrip("/")

    def _raw_backend(self) -> str:
        return (self._env.get("HERMESCLOAK_DECIDE_BACKEND") or self._backend or "jev").strip().lower()

    def local_model(self) -> str:
        return self._env.get("HERMESCLOAK_LOCAL_DECIDE_MODEL") or self._local_model or LOCAL_MODEL

    def backend(self) -> str:
        b = self._raw_backend()
        return b if b in BACKENDS else "jev"

    def daily_cap(self) -> int:
        try:
            return int(self._env.get("HERMESCLOAK_DECIDE_DAILY_CAP") or self._daily_cap or DEFAULTS["daily_cap"])
        except ValueError:
            return DEFAULTS["daily_cap"]

    def calibration(self) -> dict:
        if self._calibration is not None:
            return self._calibration
        path = self._env.get("HERMESCLOAK_DECIDE_CALIBRATION")
        if path:
            try:
                with open(path, encoding="utf-8") as fh:
                    self._calibration = json.load(fh)
            except Exception:
                self._calibration = {}
        else:
            self._calibration = {}
        return self._calibration

    def configured(self, backend: str | None = None) -> bool:
        b = backend or self.backend()
        return bool(self.key()) if b == "jev" else bool(self.local_url())

    # ---- guards ----
    def _count_ok(self) -> bool:
        with self._lock:
            today = self._today()
            if self._day["d"] != today:
                self._day.update(d=today, n=0, announced=False)
            if self._day["n"] >= self.daily_cap():
                return False
            self._day["n"] += 1
            return True

    def _cap_result(self) -> dict:
        with self._lock:
            first = not self._day["announced"]
            self._day["announced"] = True
        return {"ok": False, "reason": "daily-cap", "error": f"daily cap {self.daily_cap()} reached",
                "announce": first}

    # ---- the call ----
    def decide(self, use_case: str, state, questions: dict, timeout_s: float | None = None, *,
               backend: str | None = None, fallback: bool | None = None, shadow: bool = False,
               on_shadow=None) -> dict:
        uc = str(use_case or "").strip()
        if not uc:
            return {"ok": False, "reason": "invalid", "error": "use_case required"}
        bad = validate(questions)
        if bad:
            return {"ok": False, "reason": "invalid", "error": bad}
        primary = backend if backend in BACKENDS else self.backend()
        fb = self._fallback if fallback is None else fallback
        other = "local" if primary == "jev" else "jev"
        tmo = max(0.05, float(timeout_s or self._timeout))
        out = self._one(primary, uc, state, questions, tmo)
        used = primary
        if not out["ok"] and fb and out["reason"] not in ("invalid", "daily-cap") and self.configured(other):
            out2 = self._one(other, uc, state, questions, tmo)
            if out2["ok"]:
                out, used = {**out2, "fell_back_from": primary}, other
        if shadow and out["ok"] and on_shadow is not None and self.configured(other) and used == primary:
            self._shadow(other, uc, state, questions, tmo, out, on_shadow)
        return out

    def _one(self, backend: str, uc: str, state, questions: dict, tmo: float) -> dict:
        br = self._br[backend]
        with self._lock:
            if br.open_until > self._now():
                return {"ok": False, "reason": "breaker", "backend": backend,
                        "retry_in_s": round(br.open_until - self._now(), 1)}
        if not self.configured(backend):
            return {"ok": False, "reason": "no-key", "backend": backend}
        if backend == "local" and validate(questions, LOCAL_MAX_CHOICE):
            return {"ok": False, "reason": "invalid", "backend": backend,
                    "error": validate(questions, LOCAL_MAX_CHOICE)}
        if not self._count_ok():
            return {**self._cap_result(), "backend": backend}
        qv = {n: question_version(q) for n, q in questions.items()}
        wire = {n: _wire(q) for n, q in questions.items()}
        if backend == "jev":
            url = self.url()
            body = {"model": self.model(), "state": state, "questions": wire}
            headers = {"Content-Type": "application/json", "Authorization": "Bearer " + self.key(),
                       "X-Title": "HermesCloak decide"}
        else:
            url = self.local_url() + "/v1/systemone"
            body = {"model": self.local_model(), "keep_alive": -1, "state": state, "questions": wire}
            headers = {"Content-Type": "application/json"}
        deadline = self._now() + tmo
        if not self._slots.acquire(timeout=tmo):
            return {"ok": False, "reason": "busy", "backend": backend}
        t0 = self._now()
        with self._lock:
            self._inflight += 1
        try:
            left = max(0.05, deadline - self._now())
            status, j = self._post(url, json.dumps(body, ensure_ascii=False).encode("utf-8"), headers, left)
            if status != 200:
                msg = ""
                if isinstance(j, dict) and j.get("error"):
                    err = j["error"]
                    msg = ": " + str(err.get("message") if isinstance(err, dict) else err)[:120]
                out = {"ok": False, "reason": "error", "error": f"HTTP {status}{msg}"}
            elif not isinstance(j, dict) or not isinstance(j.get("answers"), dict):
                out = {"ok": False, "reason": "bad-response", "error": "no answers"}
            else:
                answers = calibrate(j["answers"], (self.calibration() or {}).get(uc.split(":")[0]))
                final = {}
                for n, q in questions.items():
                    a = answers.get(n)
                    if not isinstance(a, dict):
                        final[n] = None
                        continue
                    a = {**a, "qv": qv[n]}
                    mc = q.get("minConfidence")
                    if mc is not None and a.get("type", q["type"]) == "choice" or (mc is not None and "choice" in a):
                        _, p = choice({n: a}, n)
                        try:
                            if p < float(mc):
                                a["low"] = True
                        except (TypeError, ValueError):
                            pass
                    final[n] = a
                out = {"ok": True, "model": j.get("model") or (self.model() if backend == "jev" else self.local_model()),
                       "answers": final, "qv": qv}
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
            br.calls += 1
            if out["ok"]:
                br.fails = 0
                br.last_ok_at = self._now()
            else:
                br.errors += 1
                br.fails += 1
                br.last_error = out["reason"] + (": " + out["error"] if out.get("error") not in (None, out["reason"]) else "")
                if br.fails >= self._breaker_fails:
                    br.open_until = self._now() + self._breaker_s
                    br.fails = 0
        return {**out, "provider": backend, "backend": backend, "ms": ms}

    _shadow_slot = threading.BoundedSemaphore(1)

    def _shadow(self, backend, uc, state, questions, tmo, primary_out, on_shadow) -> None:
        if not Decider._shadow_slot.acquire(blocking=False):
            return

        def run():
            try:
                r = self._one(backend, "shadow:" + uc, state, questions, max(tmo, 10.0))
                on_shadow({"use_case": uc, "primary": primary_out.get("backend"),
                           "primary_answers": brief(primary_out.get("answers")),
                           "shadow": backend, "shadow_answers": brief(r.get("answers")) if r.get("ok") else None,
                           "shadow_reason": None if r.get("ok") else r.get("reason"), "shadow_ms": r.get("ms")})
            except Exception:
                pass
            finally:
                Decider._shadow_slot.release()
        threading.Thread(target=run, daemon=True).start()

    def health(self) -> dict:
        """Numbers and tags only — never a key, a state or an answer."""
        with self._lock:
            now = self._now()
            per = {}
            for b, br in self._br.items():
                per[b] = {"configured": self.configured(b), "calls": br.calls, "errors": br.errors,
                          "consecutive_fails": br.fails, "breaker_open": br.open_until > now,
                          "breaker_s_left": round(max(0.0, br.open_until - now), 1), "last_error": br.last_error,
                          "last_ok_s_ago": None if br.last_ok_at is None else round(now - br.last_ok_at, 1)}
            return {"backend": self.backend(), "url": self.url(), "model": self.model(),
                    "local_url": self.local_url(), "local_model": self.local_model(), "has_key": bool(self.key()),
                    "inflight": self._inflight, "concurrency": self._concurrency,
                    "today_calls": self._day["n"], "daily_cap": self.daily_cap(), "backends": per}


_shared: Decider | None = None
_shared_lock = threading.Lock()


def shared() -> Decider:
    """One process-wide Decider, so breakers, the daily count and the concurrency cap are shared."""
    global _shared
    with _shared_lock:
        if _shared is None:
            _shared = Decider()
        return _shared


def decide(use_case: str, state, questions: dict, timeout_s: float | None = None, **kw) -> dict:
    return shared().decide(use_case, state, questions, timeout_s, **kw)


def health() -> dict:
    return shared().health()
