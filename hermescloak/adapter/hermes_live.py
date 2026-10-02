"""Live integration shim for hermes-agent. The HermesCloak hermes plugin
(``hermescloak.hermes_plugin``) calls these; so do the legacy source-insertion seams.

Scoped + safe by design:
  * MODE from `$HERMES_HOME/cloak/MODE` ∈ {absent|off|shadow|enforce}. Absent/off → passthrough,
    so any agent without the cloak dir is unaffected (scopes the deployment to one agent).
  * shadow → run detection to PROVE it works (audit logs counts/types only, NEVER real PII) but
    SEND THE ORIGINAL outbound and restore nothing (zero behaviour change).
  * enforce → tokenize the outbound copy; restore the response (content + tool-call args) in place.
  * FAIL-OPEN by default: any exception → the original + an audit line; never raise into hermes.
    ``fail_mode: closed`` withholds the text instead.
  * SELF-HEALING config: a broken profile.yaml / gazetteer.txt never disables filtering — the
    last good version keeps being used (audited as ``config_error``); edits apply live.

Duck-typed on the passed objects (no hermes import) so it stays testable and import-safe.
The audit log never contains real PII — only event kinds, counts, and entity types."""
import json
import os
import threading
import time
from collections import OrderedDict
from pathlib import Path

from hermescloak.adapter.alerts import AlertEvent, FileAuditAlerter
from hermescloak.engine import Engine
from hermescloak.entities import StaticFileSource
from hermescloak.profile import Profile

_LOCK = threading.Lock()
# One Engine per (home, session). Bounded LRU: a gateway that runs for weeks sees thousands of
# sessions, and each Engine holds a content cache — the old plain dict grew without limit.
_ENGINES: "OrderedDict[tuple[str, str], Engine]" = OrderedDict()
_ENGINE_SIG: dict = {}             # (home, session) -> config signature the engine was built from
_MAX_ENGINES = 128
_LAST_GOOD: dict = {}              # (home, "profile"|"gazetteer") -> last successfully loaded value


def _home() -> str:
    """The ACTIVE hermes home. Current hermes resolves it as context-local override (profiles,
    multi-tenant gateway) → $HERMES_HOME → platform default (%LOCALAPPDATA% on Windows), so
    reading only $HERMES_HOME/~/.hermes pointed at the wrong cloak dir under profiles."""
    try:
        from hermes_constants import get_hermes_home  # present when running inside hermes
        home = str(get_hermes_home())
    except Exception:
        home = os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes")
    # normalized: the per-agent vault file is keyed by this string ("a/../b" ≠ "b" made 2 vaults)
    return os.path.realpath(os.path.expanduser(home))


def _cloak_dir() -> Path:
    return Path(_home()) / "cloak"


def _mode() -> str:
    try:
        return (_cloak_dir() / "MODE").read_text(encoding="utf-8").strip().lower()
    except Exception:
        return "off"


def mode() -> str:
    """Public: the live MODE (off | shadow | enforce) for the active hermes home."""
    m = _mode()
    return m if m in ("shadow", "enforce") else "off"


# ---------------------------------------------------------------- audit

_AUDIT_MAX_BYTES = 5 * 1024 * 1024
_AUDIT_KEEP = 3
_RATE: dict = {}                   # (home, kind) -> last time, for noisy repeated incidents


def _audit(kind: str, detail: str, *, every: float = 0.0) -> None:
    """Append one JSONL event (timestamped, no PII). Rotates at 5 MB, keeps 3 old files.
    ``every`` rate-limits a repeating incident to one line per that many seconds."""
    try:
        home = _home()
        if every:
            now = time.monotonic()
            if now - _RATE.get((home, kind), -1e9) < every:
                return
            _RATE[(home, kind)] = now
        path = str(_cloak_dir() / "audit.log")
        try:
            if os.path.getsize(path) > _AUDIT_MAX_BYTES:
                for i in range(_AUDIT_KEEP - 1, 0, -1):
                    if os.path.exists(f"{path}.{i}"):
                        os.replace(f"{path}.{i}", f"{path}.{i + 1}")
                os.replace(path, f"{path}.1")
        except OSError:
            pass
        FileAuditAlerter(path).send(AlertEvent(kind, home, detail))
    except Exception:
        pass


def _drain_vault_events(v) -> None:
    for ev in list(getattr(v, "events", []) or []):
        _audit(ev, json.dumps({"conflicts": getattr(v, "conflicts", 0)}))
    try:
        v.events.clear()
    except Exception:
        pass


# ---------------------------------------------------------------- config (self-healing)

def _sig(d: Path) -> tuple:
    out = []
    for name in ("profile.yaml", "gazetteer.txt", "ner_url"):
        try:
            st = (d / name).stat()
            out.append((name, st.st_mtime_ns, st.st_size))
        except OSError:
            out.append((name, None, None))
    return tuple(out)


class _NamesSource:
    def __init__(self, names):
        self._names = names

    def names(self):
        return list(self._names)


def _load_profile(home: str, d: Path) -> Profile:
    p = d / "profile.yaml"
    if not p.exists():
        return Profile(name="default")
    try:
        prof = Profile.from_yaml(str(p))
        _LAST_GOOD[(home, "profile")] = prof
        return prof
    except Exception as exc:
        _audit("config_error", json.dumps({"file": "profile.yaml", "error": type(exc).__name__,
                                           "using": "last-good" if (home, "profile") in _LAST_GOOD
                                           else "defaults"}), every=60)
        return _LAST_GOOD.get((home, "profile")) or Profile(name="default")


def _load_gazetteer(home: str, d: Path):
    g = d / "gazetteer.txt"
    if not g.exists():
        return None
    try:
        names = list(StaticFileSource(str(g)).names())
        _LAST_GOOD[(home, "gazetteer")] = names
        return _NamesSource(names)
    except Exception as exc:
        prev = _LAST_GOOD.get((home, "gazetteer"))
        _audit("config_error", json.dumps({"file": "gazetteer.txt", "error": type(exc).__name__,
                                           "using": "last-good" if prev else "none"}), every=60)
        return _NamesSource(prev) if prev else None


def _build_engine(session_id: str = "default") -> Engine:
    home = _home()
    d = _cloak_dir()
    profile = _load_profile(home, d)
    source = _load_gazetteer(home, d)
    extra: list = []
    try:
        url = (d / "ner_url").read_text(encoding="utf-8").strip() if (d / "ner_url").exists() else ""
    except Exception:
        url = ""
    if url:                                   # guard: empty ner_url → no NER (don't wire a dead client)
        from hermescloak.adapter.ner_client import NerServiceRecognizer
        extra.append(NerServiceRecognizer(
            url, control_file=str(d / "ner.ctl"),
            on_state_change=lambda up: _audit("ner_up" if up else "ner_down", url and "{}")))
    return Engine(profile, entity_source=source, extra_recognizers=extra or None,
                  vault=_agent_vault(profile))


# ---------------------------------------------------------------- vault + replay (one per agent)

# ONE durable vault PER AGENT (keyed by HERMES_HOME), shared across all of that agent's
# sessions and processes and reloaded on restart. This is what guarantees no broken
# deliverables: every token the agent ever issued stays restorable across sessions AND gateway
# restarts. Opt out with "memory" in $HERMES_HOME/cloak/vault_mode. Fail-open to in-memory.
_AGENT_VAULTS: dict = {}
_REPLAYS: dict = {}
_SHADOW_VAULTS: dict = {}      # shadow-mode counting only; never persisted
_VLOCK = threading.Lock()      # separate from _LOCK: _engine_for holds _LOCK when it calls here


def _vault_key():
    """Fernet key for encryption at rest: $HERMESCLOAK_VAULT_KEY, or the file named by
    $HERMESCLOAK_VAULT_KEY_FILE or by $HERMES_HOME/cloak/vault_key_file (keep the key itself
    OUTSIDE the cloak dir). None → plaintext (0600) vault."""
    k = os.environ.get("HERMESCLOAK_VAULT_KEY", "").strip()
    if k:
        return k
    ref = os.environ.get("HERMESCLOAK_VAULT_KEY_FILE", "").strip()
    if not ref:
        try:
            ref = (_cloak_dir() / "vault_key_file").read_text(encoding="utf-8").strip()
        except Exception:
            ref = ""
    if ref:
        try:
            return Path(os.path.expanduser(ref)).read_text(encoding="utf-8").strip() or None
        except Exception:
            _audit("vault_key_unreadable", "{}", every=300)
            return "!unreadable"      # forces memory-only: never write plaintext when a key was asked for
    return None


def _agent_vault(profile: Profile | None = None):
    home = _home()
    with _VLOCK:
        v = _AGENT_VAULTS.get(home)
        if v is None:
            try:
                d = _cloak_dir()
                mode_f = d / "vault_mode"
                if mode_f.exists() and mode_f.read_text(encoding="utf-8").strip().lower() == "memory":
                    from hermescloak.vault import Vault
                    v = Vault()                       # opt-out: in-memory
                else:
                    from hermescloak.durable_vault import DurableVault
                    ttl = int(3600 * float(getattr(profile, "vault_ttl_hours", 24.0) or 24.0))
                    vdir = d / "vaults"
                    DurableVault.sweep_expired(str(vdir), ttl)
                    v = DurableVault(DurableVault.path_for(str(vdir), home), ttl_seconds=ttl,
                                     key=_vault_key())
            except Exception as exc:
                from hermescloak.vault import Vault
                v = Vault()                           # fail-open
                _audit("vault_error", repr(exc))
            _AGENT_VAULTS[home] = v
        _drain_vault_events(v)
        return v


def _replay():
    home = _home()
    with _VLOCK:
        r = _REPLAYS.get(home)
        if r is None:
            from hermescloak.replay import ReplayCache
            r = ReplayCache(str(_cloak_dir() / "replay.json"))
            _REPLAYS[home] = r
        return r


def _generation(vault) -> str:
    return getattr(vault, "generation", "") or f"mem-{id(vault)}"


def _engine_for_session(session_id) -> Engine:
    home = _home()
    key = (home, str(session_id or "default"))
    sig = _sig(Path(home) / "cloak")
    with _LOCK:
        eng = _ENGINES.get(key)
        if eng is None or _ENGINE_SIG.get(key) != sig:
            eng = _build_engine(key[1])               # shares the per-agent vault; picks up edits live
            _ENGINES[key] = eng
            _ENGINE_SIG[key] = sig
            while len(_ENGINES) > _MAX_ENGINES:
                old, _ = _ENGINES.popitem(last=False)
                _ENGINE_SIG.pop(old, None)
        else:
            _ENGINES.move_to_end(key)
        return eng


def _engine_for(agent) -> Engine:
    return _engine_for_session(getattr(agent, "session_id", None))


def _fail_closed() -> bool:
    try:
        return _load_profile(_home(), _cloak_dir()).fail_mode == "closed"
    except Exception:
        return False


_REDACTED = "[HermesCloak: content withheld — PII filter failed (fail_mode: closed)]"


# ---------------------------------------------------------------- outbound

def cloak_sanitize_request(request: dict, session_id=None, api_mode: str = ""):
    """Tokenize a PROVIDER-SHAPED request (hermes ``llm_request`` middleware payload).

    Returns the request to send: a tokenized copy in enforce, the original in off/shadow.
    The model's own earlier turns are replayed byte-identically (ReplayCache).
    On an internal error: the original (fail_mode: open, default) or a copy whose text is
    replaced by a notice (fail_mode: closed) — never raises."""
    m = _mode()
    if m not in ("shadow", "enforce") or not isinstance(request, dict):
        return request
    from hermescloak.adapter import payload as _p
    from hermescloak.restorer import leftover_tokens
    try:
        eng = _engine_for_session(session_id)
        if m == "shadow":
            # count what WOULD be masked with a throwaway in-memory vault: shadow must not write
            # a real-value store to disk or change anything the agent sees
            from hermescloak.pseudonymizer import pseudonymize
            from hermescloak.vault import Vault
            sv = _SHADOW_VAULTS.setdefault(_home(), Vault())
            _p.transform_request(request, lambda s: pseudonymize(s, eng.detection, sv))
            _audit("shadow_detect", json.dumps(sv.summary(), ensure_ascii=False))
            return request  # zero behaviour change — original goes to the cloud
        fn = eng.tokenize_text
        raw_args = None
        replayed = [0]
        if m == "enforce" and eng.profile.replay_cache:
            rc, gen, vault = _replay(), _generation(eng.vault), eng.vault

            def _replayed(s):
                hit = rc.lookup(s, gen)
                if hit is not None and not leftover_tokens(hit, vault):
                    replayed[0] += 1
                    return hit
                return None

            def fn(s, _tok=eng.tokenize_text):           # noqa: F811
                hit = _replayed(s)
                return hit if hit is not None else _tok(s)

            raw_args = _replayed
        sanitized = _p.transform_request(request, fn, raw_args=raw_args)
        if eng.profile.token_instruction and not eng.vault.is_empty():
            from hermescloak.instruction import TOKEN_INSTRUCTION
            sanitized = _p.inject_instruction(sanitized, TOKEN_INSTRUCTION, api_mode)
        getattr(eng.vault, "save", lambda: None)()
        _drain_vault_events(eng.vault)
        _audit("enforce_send", json.dumps(
            {"entities": eng.vault.summary(),
             "real_values_in_outbound": eng.vault.count_present(_p.request_text_blob(sanitized)),
             "replayed": replayed[0], "api_mode": api_mode or "?"},
            ensure_ascii=False))
        return sanitized
    except Exception as exc:  # noqa: BLE001
        if m == "enforce" and _fail_closed():
            _audit("blocked_send", repr(exc))
            try:
                return _p.transform_request(request, lambda _s: _REDACTED)
            except Exception:
                return {**request, "messages": [{"role": "user", "content": _REDACTED}]}
        _audit("unfiltered_sent", repr(exc))
        return request


def cloak_sanitize_outbound(agent, api_messages):
    """Legacy seam A (chat-shaped message list)."""
    out = cloak_sanitize_request({"messages": api_messages}, getattr(agent, "session_id", None),
                                 "chat_completions")
    return out["messages"] if isinstance(out, dict) and "messages" in out else api_messages


# ---------------------------------------------------------------- inbound

def _restore_args_string(raw, vault, tolerant=False):
    """Restore tokens in a JSON-encoded tool-arguments string, incl. ``\\\\u27e6``-escaped ones."""
    from hermescloak.adapter.payload import transform_json_string, unescape_tokens
    from hermescloak.restorer import restore_text
    if not isinstance(raw, str) or not raw:
        return raw
    if "⟦" not in raw and "\\u27e6" not in raw.lower() and not tolerant:
        return raw
    # json.loads decodes \\u27e6-escaped tokens; unparseable args fall back to text restore
    # after un-escaping the bracket pair.
    restore = lambda t: restore_text(unescape_tokens(t), vault, tolerant)  # noqa: E731
    return transform_json_string(raw, restore)


def _audit_model_output(eng: Engine, model_texts: list[str]) -> None:
    """veil-style output audit on what the MODEL wrote (pre-restore), counts/types only:
      leaked_original → a real value the vault masked appears verbatim: the model saw it some
                        other way (an unmasked path) — investigate.
      new_pii         → PII-shaped values the model introduced (invented or echoed)."""
    try:
        from hermescloak.tokens import TOKEN_RE
        vault = eng.vault
        reals = set(getattr(vault, "_token_to_real", {}).values())
        leaked: dict = {}
        new: dict = {}
        for t in model_texts:
            if not t:
                continue
            for s in eng.detection.detect_primary(t):
                if TOKEN_RE.fullmatch(s.text):
                    continue
                bucket = leaked if s.text in reals else new
                bucket[s.entity_type] = bucket.get(s.entity_type, 0) + 1
        if leaked:
            _audit("leaked_original", json.dumps(leaked, ensure_ascii=False))
        if new:
            _audit("new_pii", json.dumps(new, ensure_ascii=False))
    except Exception:
        pass


def cloak_restore_normalized(response):
    """Restore real values IN PLACE on a hermes ``NormalizedResponse`` (any transport):
    content, reasoning, and every tool call's arguments — before hermes persists, displays,
    or executes anything. Remembers each (restored → model text) pair for byte-identical
    replay. Enforce-only, fail-open, returns the same object."""
    if _mode() != "enforce" or response is None:
        return response
    try:
        from hermescloak.restorer import leftover_tokens, restore_text
        eng = _engine_for_session(None)
        vault = eng.vault
        prof = eng.profile
        tol = bool(prof.tolerant_restore)
        rc = _replay() if prof.replay_cache else None
        gen = _generation(vault)
        surfaces: list[str] = []
        model_texts: list[str] = []
        for attr in ("content", "reasoning"):
            v = getattr(response, attr, None)
            if isinstance(v, str):
                model_texts.append(v)
                if not vault.is_empty():
                    new = restore_text(v, vault, tol)
                    if new != v:
                        setattr(response, attr, new)
                        if rc is not None:
                            rc.remember(new, v, gen)
                    v = new
                surfaces.append(v)
        for tc in list(getattr(response, "tool_calls", None) or []):
            fn = getattr(tc, "function", None) or tc
            args = fn.get("arguments") if isinstance(fn, dict) else getattr(fn, "arguments", None)
            if isinstance(args, str):
                model_texts.append(args)
            new = _restore_args_string(args, vault, tol) if not vault.is_empty() else args
            if new is not args and new != args:
                if isinstance(fn, dict):
                    fn["arguments"] = new
                else:
                    fn.arguments = new
                if rc is not None:
                    rc.remember(new, args, gen)
            surfaces.append(str(new or ""))
        if rc is not None:
            rc.save()
        if prof.audit_new_pii:
            _audit_model_output(eng, model_texts)
        if vault.is_empty():
            return response
        leftover = sorted({t for s in surfaces for t in leftover_tokens(s, vault, tol)})
        _audit("enforce_restore", json.dumps({"restored": True, "leftover": len(leftover)},
                                              ensure_ascii=False))
        if leftover:
            _audit("leftover_token", ",".join(leftover))
    except Exception as exc:  # noqa: BLE001 — fail-open
        _audit("restore_error", repr(exc))
    return response


class _DictView:
    """Attribute view over a message dict so cloak_restore_normalized can treat it uniformly."""

    def __init__(self, d: dict) -> None:
        object.__setattr__(self, "_d", d)

    def __getattr__(self, name):
        return self._d.get(name)

    def __setattr__(self, name, value):
        self._d[name] = value


def cloak_restore_chat_response(response):
    """Restore an OpenAI-shaped ChatCompletion (auxiliary calls) in place: every
    ``choices[].message`` content + tool-call arguments. Enforce-only, fail-open."""
    if _mode() != "enforce" or response is None:
        return response
    try:
        choices = response.get("choices") if isinstance(response, dict) else getattr(response, "choices", None)
        for ch in choices or []:
            msg = ch.get("message") if isinstance(ch, dict) else getattr(ch, "message", None)
            if msg is None:
                continue
            cloak_restore_normalized(_DictView(msg) if isinstance(msg, dict) else msg)
    except Exception as exc:  # noqa: BLE001
        _audit("restore_error", repr(exc))
    return response


def cloak_restore_text_for(agent, text):
    """Last-mile restore: rehydrate any ⟦token⟧ in an outgoing user-facing string.
    Fail-open; only acts in enforce when a token is present, so it's cheap and safe on every send."""
    if _mode() != "enforce" or not isinstance(text, str) or not any(c in text for c in "⟦⟨【["):
        return text
    try:
        from hermescloak.restorer import restore_text
        eng = _engine_for(agent)
        return restore_text(text, eng.vault, bool(eng.profile.tolerant_restore))
    except Exception:
        return text


def cloak_restore_args_dict(args):
    """Restore every string leaf of a tool-arguments dict (the ``tool_request`` backstop)."""
    if _mode() != "enforce" or not isinstance(args, dict):
        return args
    try:
        if "⟦" not in json.dumps(args, ensure_ascii=False) and not any(
                c in json.dumps(args, ensure_ascii=False) for c in "⟨【["):
            return args
        from hermescloak.restorer import restore_json
        eng = _engine_for_session(None)
        return restore_json(args, eng.vault, bool(eng.profile.tolerant_restore))
    except Exception:
        return args


def cloak_restore_inbound(agent, assistant_message):
    """Legacy seam B."""
    return cloak_restore_normalized(assistant_message)


# ---------------------------------------------------------------- streaming

_MAX_TOKEN_HOLD = 48  # never hold back more than a plausible token's length


def cloak_filter_stream_delta(agent, state, text):
    """Restore ⟦tokens⟧ in a streaming delta BEFORE it reaches the consumer. Buffers an
    unmatched '⟦' until its closing '⟧' arrives (tokens can split across deltas); the caller
    flushes the remainder at stream end (cloak_flush_stream). Exact restore only — tolerant
    rewriting is unsafe on partial text (the final message is restored tolerantly).
    Fail-open: on any error, returns the original text."""
    if _mode() != "enforce":
        return text
    try:
        buf = state.get("buf", "") + (text or "")
        open_idx = buf.rfind("⟦")
        # hold back a trailing, still-open token (no ⟧ yet) only if it's short enough
        if open_idx != -1 and "⟧" not in buf[open_idx:] and (len(buf) - open_idx) <= _MAX_TOKEN_HOLD:
            emit, hold = buf[:open_idx], buf[open_idx:]
        else:
            emit, hold = buf, ""
        state["buf"] = hold
        if not emit:
            return ""
        from hermescloak.restorer import restore_text
        return restore_text(emit, _engine_for(agent).vault)
    except Exception:
        return text


def cloak_flush_stream(agent, state):
    """End of a stream: release (restored) any text still held back waiting for a ``⟧``.
    Without this, a reply ending in an unmatched ``⟦`` silently lost its tail."""
    try:
        held = state.pop("buf", "")
        if not held:
            return ""
        from hermescloak.restorer import restore_text
        return restore_text(held, _engine_for(agent).vault) if _mode() == "enforce" else held
    except Exception:
        return ""


# ---------------------------------------------------------------- self-test

def self_test() -> tuple[bool, str]:
    """Round-trip a synthetic canary through a throwaway in-memory engine (never the agent's
    vault). Proves the detector/tokenizer/restorer are functional in THIS process."""
    try:
        from hermescloak.adapter import payload as _p
        from hermescloak.restorer import restore_text
        from hermescloak.vault import Vault
        eng = Engine(Profile(name="selftest"), vault=Vault())
        canary = "selftest canary@example.org 050-1234567 password=Zx9-canary-Q"
        req = {"messages": [{"role": "user", "content": [{"type": "text", "text": canary}]}]}
        out = _p.transform_request(req, eng.tokenize_text)
        sent = out["messages"][0]["content"][0]["text"]
        if any(v in sent for v in ("canary@example.org", "050-1234567", "Zx9-canary-Q")):
            return False, "tokenizer left a canary value in the outbound copy"
        if restore_text(sent, eng.vault) != canary:
            return False, "restore did not round-trip"
        return True, "ok"
    except Exception as exc:  # noqa: BLE001
        return False, repr(exc)
