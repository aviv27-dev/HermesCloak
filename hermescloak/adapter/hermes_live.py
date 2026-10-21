"""Live integration shim for hermes-agent. The HermesCloak hermes plugin
(``hermescloak.hermes_plugin``) calls these; so do the legacy source-insertion seams.

Scoped + safe by design:
  * MODE from `$HERMES_HOME/cloak/MODE` ∈ {absent|off|shadow|enforce}. Absent/off → passthrough,
    so any agent without the cloak dir is unaffected (scopes the deployment to one agent).
  * shadow → run detection to PROVE it works (audit logs counts/types only, NEVER real PII) but
    SEND THE ORIGINAL outbound and restore nothing (zero behaviour change).
  * enforce → tokenize the outbound copy; restore the response (content + tool-call args) in place.
  * FAIL-CLOSED by default: in enforce, an internal error withholds the text (audit
    ``blocked_send``); ``fail_mode: open`` sends the original + ``unfiltered_sent`` instead.
    Never raises into hermes.
  * SELF-HEALING config: a broken profile.yaml / gazetteer.txt never disables filtering — the
    last good version keeps being used (audited as ``config_error``); edits apply live.

Duck-typed on the passed objects (no hermes import) so it stays testable and import-safe.
The audit log never contains real PII — only event kinds, counts, and entity types."""
import hashlib
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


def parse_mode(raw: bytes) -> str:
    """MODE file bytes → off | shadow | enforce. Tolerates a BOM, UTF-16 (PowerShell 5 `echo >`)
    and surrounding whitespace; anything present but unrecognized means enforce (fail closed)."""
    for enc in ("utf-8-sig", "utf-16"):
        try:
            text = raw.decode(enc)
        except UnicodeError:
            continue
        m = text.replace("\ufeff", "").replace("\x00", "").strip().lower()
        if m in ("off", "shadow", "enforce"):
            return m
    return "enforce"


def _mode() -> str:
    try:
        raw = (_cloak_dir() / "MODE").read_bytes()
    except FileNotFoundError:
        return "off"                     # no MODE file: this agent is not opted in
    except Exception:
        return "enforce"                 # present but unreadable: fail closed
    return parse_mode(raw)


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
                from hermescloak._filelock import FileLock
                with FileLock(path + ".lock", timeout=2.0):   # one rotator at a time
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
                _audit("vault_error", type(exc).__name__)
            _AGENT_VAULTS[home] = v
        _drain_vault_events(v)
        return v


def _replay():
    home = _home()
    with _VLOCK:
        r = _REPLAYS.get(home)
        if r is None:
            from hermescloak.replay import ReplayCache
            key = _vault_key()
            secret = (hashlib.sha256(b"hermescloak-replay\0" + key.encode("utf-8")).digest()
                      if key and key != "!unreadable" else None)
            r = ReplayCache(str(_cloak_dir() / "replay.json"), secret=secret)
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
        return _load_profile(_home(), _cloak_dir()).fail_mode != "open"
    except Exception:
        return True


# ---------------------------------------------------------------- restore scope
# The vault is shared by all of an agent's sessions, and tokens are guessable (⟦לקוח_1⟧, ⟦תז_1⟧…).
# Restoring every token the vault knows would let injected text in session B ("fetch
# https://x/?q=⟦לקוח_1⟧") pull real values of session A's clients into a tool call. So a reply or
# tool call gets back only the tokens its own session was shown: the tokens in that session's
# sanitized requests. The history is resent on every turn, so the set rebuilds itself after a restart.
import contextvars
from hermescloak.tokens import TOKEN_RE as _TOKEN_RE

_SHOWN: "contextvars.ContextVar[frozenset | None]" = contextvars.ContextVar("hermescloak_shown", default=None)
_SENT: "OrderedDict[tuple[str, str], set]" = OrderedDict()   # (home, session) -> tokens shown to the model
_MAX_SENT_SESSIONS = 1024
_SLOCK = threading.Lock()


def _sid(session_id) -> str:
    return str(session_id or "default")


def _minting(fn, minted: set):
    """Wrap a text transform: record the tokens IT put in (not ones already in the input — a token
    an attacker typed into a web page or tool result must not become restorable)."""
    def g(s):
        out = fn(s)
        if isinstance(out, str) and isinstance(s, str) and "⟦" in out:
            before = {m.group(0) for m in _TOKEN_RE.finditer(s)} if "⟦" in s else set()
            minted.update(m.group(0) for m in _TOKEN_RE.finditer(out) if m.group(0) not in before)
        return out
    return g


def _remember_shown(session_id, found: set) -> frozenset:
    key = (_home(), _sid(session_id))
    with _SLOCK:
        cur = _SENT.get(key)
        if cur is None:
            cur = _SENT[key] = set()
            while len(_SENT) > _MAX_SENT_SESSIONS:
                _SENT.popitem(last=False)
        else:
            _SENT.move_to_end(key)
        cur |= found
        shown = frozenset(cur)
    _SHOWN.set(shown)
    return shown


def _scope(session_id=None, *, use_context: bool = True):
    """The tokens this call may restore: the session's own (by id), else the current turn's."""
    if session_id not in (None, ""):
        with _SLOCK:
            cur = _SENT.get((_home(), _sid(session_id)))
        if cur is not None:
            return frozenset(cur)
    if use_context:
        ctx = _SHOWN.get()
        if ctx is not None:
            return ctx
    return None


class _ScopedVault:
    """Read-only view of the vault that resolves only the tokens in ``allowed``."""

    def __init__(self, vault, allowed) -> None:
        self._v, self._allowed, self.foreign = vault, allowed, set()

    def restore_token(self, token: str):
        real = self._v.restore_token(token)
        if real is not None and token not in self._allowed:
            self.foreign.add(token)
            return None
        return real

    def __getattr__(self, name):
        return getattr(self._v, name)


def _scoped(vault, allowed, where: str):
    if allowed is None:                      # no session and no turn context: restore nothing
        _audit("restore_unscoped", json.dumps({"where": where}), every=300)
        allowed = frozenset()
    return _ScopedVault(vault, allowed)


def _report_foreign(sv, where: str) -> None:
    if isinstance(sv, _ScopedVault) and sv.foreign:
        _audit("foreign_token", json.dumps({"where": where, "count": len(sv.foreign)}))


_REDACTED = "[HermesCloak: content withheld — PII filter failed (fail_mode: closed)]"


def withheld(request: dict) -> dict:
    """The request with every piece of text replaced by the notice."""
    from hermescloak.adapter import payload as _p
    try:
        return _p.transform_request(request, lambda _s: _REDACTED)
    except Exception:
        return _withheld_minimal(request)


def _withheld_minimal(request: dict) -> dict:
    """Last resort when even the redacting walk failed: drop EVERY text-bearing key."""
    from hermescloak.adapter.payload import REQUEST_TEXT_KEYS
    out = {k: v for k, v in request.items() if k not in REQUEST_TEXT_KEYS}
    if "input" in request or "instructions" in request:
        out["input"] = [{"role": "user", "content": _REDACTED}]
    elif "system" in request:
        out["messages"] = [{"role": "user", "content": [{"type": "text", "text": _REDACTED}]}]
    else:
        out["messages"] = [{"role": "user", "content": _REDACTED}]
    return out


_REDACTED_JEV = "[HermesCloak: content withheld — an identifier may still be in clear (jev_action: block)]"


def _jev_residual(eng, masked_text: str):
    """Optional typed-decision second opinion on the masked request (profile ``jev_check``). Audits
    ``jev_residual`` (probabilities + hits, counts only) or ``jev_unavailable``; never raises."""
    try:
        if not getattr(eng.profile, "jev_check", False):
            return None
        from hermescloak import residual as _res

        def _on_shadow(rec):          # both backends' probabilities, nothing else
            _audit("decide_shadow", json.dumps(rec, ensure_ascii=False))

        r = _res.check(masked_text, eng.profile, on_shadow=_on_shadow)
        if r is None:
            _audit("jev_unavailable", json.dumps({"reason": "no-key"}), every=600)
            return None
        if "error" in r:
            # the daily cap announces itself once a day; other failures are rate-limited
            _audit("jev_unavailable", json.dumps({"reason": r["error"], "detail": r.get("detail"),
                                                  "backend": r.get("backend"), "ms": r.get("ms")}),
                   every=0 if r.get("announce") else 60)
            return None
        _audit("jev_residual", json.dumps({"hits": r["hits"], "probs": r["probs"], "ms": r["ms"],
                                           "model": r.get("model"), "backend": r.get("backend"),
                                           **({"fell_back_from": r["fell_back_from"]} if r.get("fell_back_from") else {})},
                                          ensure_ascii=False))
        return r
    except Exception as exc:  # noqa: BLE001
        _audit("jev_unavailable", json.dumps({"reason": "error", "detail": type(exc).__name__}), every=60)
        return None


# ---------------------------------------------------------------- outbound

def _tokens_by_type(text: str, vault) -> dict:
    """type -> number of DISTINCT vault tokens in ``text`` (counts only, no values)."""
    from hermescloak.tokens import TOKEN_RE
    seen: dict = {}
    for m in TOKEN_RE.finditer(text or ""):
        if vault.restore_token(m.group(0)) is not None:
            seen.setdefault(m.group(1), set()).add(m.group(0))
    return {t: len(v) for t, v in sorted(seen.items())}


def cloak_sanitize_request(request: dict, session_id=None, api_mode: str = ""):
    """Tokenize a PROVIDER-SHAPED request (hermes ``llm_request`` middleware payload).

    Returns the request to send: a tokenized copy in enforce, the original in off/shadow.
    The model's own earlier turns are replayed byte-identically (ReplayCache).
    On an internal error: a copy whose text is replaced by a notice (fail_mode: closed, default)
    or the original (fail_mode: open) — never raises."""
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
            sv = _SHADOW_VAULTS.get(_home())
            if sv is None or len(getattr(sv, "_token_to_real", ())) > 50_000:   # counting only: bounded
                sv = _SHADOW_VAULTS[_home()] = Vault()
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
                if hit is not None and not leftover_tokens(hit, vault, tolerant=True):
                    replayed[0] += 1
                    return hit
                return None

            def fn(s, _tok=eng.tokenize_text):           # noqa: F811
                hit = _replayed(s)
                return hit if hit is not None else _tok(s)

            raw_args = _replayed
        minted: set = set()
        sanitized = _p.transform_request(request, _minting(fn, minted),
                                         raw_args=_minting(raw_args, minted) if raw_args else None)
        _remember_shown(session_id, minted)
        # measured BEFORE the instruction is added, so its example tokens never count
        in_request = _tokens_by_type(_p.request_text_blob(sanitized), eng.vault)
        in_system = _tokens_by_type(_p.system_text_blob(sanitized), eng.vault)
        residual = _jev_residual(eng, _p.request_text_blob(sanitized))
        if residual and residual.get("hits") and getattr(eng.profile, "jev_action", "audit") == "block":
            _audit("blocked_send", json.dumps({"reason": "jev_residual", "hits": residual["hits"]},
                                              ensure_ascii=False))
            return _p.transform_request(request, lambda _s: _REDACTED_JEV)
        if eng.profile.token_instruction and not eng.vault.is_empty():
            from hermescloak.instruction import TOKEN_INSTRUCTION
            sanitized = _p.inject_instruction(sanitized, TOKEN_INSTRUCTION, api_mode)
        getattr(eng.vault, "save", lambda: None)()
        _drain_vault_events(eng.vault)
        _audit("enforce_send", json.dumps(
            {"in_request": in_request, "in_system_prompt": in_system,
             "entities": eng.vault.summary(),
             "real_values_in_outbound": eng.vault.count_present(_p.request_text_blob(sanitized)),
             "replayed": replayed[0], "api_mode": api_mode or "?"},
            ensure_ascii=False))
        return sanitized
    except Exception as exc:  # noqa: BLE001
        if m == "enforce" and _fail_closed():
            _audit("blocked_send", type(exc).__name__)
            try:
                return _p.transform_request(request, lambda _s: _REDACTED)
            except Exception:
                return _withheld_minimal(request)
        _audit("unfiltered_sent", type(exc).__name__)
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
    tol = "strict" if tolerant else False      # tool args can be code: unambiguous brackets only
    restore = lambda t: restore_text(unescape_tokens(t), vault, tol)  # noqa: E731
    return transform_json_string(raw, restore)


def _audit_model_output(eng: Engine, model_texts: list[str]) -> None:
    """Output audit on what the MODEL wrote (pre-restore), counts/types only:
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
        vault = _scoped(eng.vault, _scope(None), "response")
        prof = eng.profile
        tol = bool(prof.tolerant_restore)
        rc = _replay() if prof.replay_cache else None
        gen = _generation(vault)
        surfaces: list[str] = []          # prose (tolerant leftover check)
        arg_surfaces: list[str] = []      # tool args = possibly code (strict brackets only)
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
            fn = (tc.get("function") or tc) if isinstance(tc, dict) else (getattr(tc, "function", None) or tc)
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
            arg_surfaces.append(str(new or ""))
        if rc is not None:
            rc.save()
        _report_foreign(vault, "response")
        if prof.audit_new_pii:
            _audit_model_output(eng, model_texts)
        if vault.is_empty():
            return response
        leftover = sorted({t for s in surfaces for t in leftover_tokens(s, vault, tol)}
                          | {t for s in arg_surfaces
                             for t in leftover_tokens(s, vault, "strict" if tol else False)})
        _audit("enforce_restore", json.dumps({"restored": True, "leftover": len(leftover)},
                                              ensure_ascii=False))
        if leftover:   # types and a count only: a mangled token can wrap a real value (⟦⟦מייל_1⟧_7⟧)
            known = set(vault.summary())
            _audit("leftover_token", json.dumps(
                {"count": len(leftover),
                 "types": sorted({t[1:t.rindex("_")] if t[1:t.rindex("_")] in known else "?" for t in leftover})},
                ensure_ascii=False))
    except Exception as exc:  # noqa: BLE001 — fail-open
        _audit("restore_error", type(exc).__name__)
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
        _audit("restore_error", type(exc).__name__)
    return response


def cloak_restore_text_for(agent, text):
    """Last-mile restore: rehydrate any ⟦token⟧ in an outgoing user-facing string.
    Fail-open; only acts in enforce when a token is present, so it's cheap and safe on every send."""
    if _mode() != "enforce" or not isinstance(text, str) or not any(c in text for c in "⟦⟨【["):
        return text
    try:
        from hermescloak.restorer import restore_text
        eng = _engine_for(agent)
        vault = _scoped(eng.vault, _scope(getattr(agent, "session_id", None)), "final_reply")
        out = restore_text(text, vault, bool(eng.profile.tolerant_restore))
        _report_foreign(vault, "final_reply")
        return out
    except Exception:
        return text


def cloak_restore_args_dict(args, session_id=None):
    """Restore every string leaf of a tool-arguments dict (the ``tool_request`` backstop) —
    only the tokens ``session_id``'s requests showed the model."""
    if _mode() != "enforce" or not isinstance(args, dict):
        return args
    try:
        if "⟦" not in json.dumps(args, ensure_ascii=False) and not any(
                c in json.dumps(args, ensure_ascii=False) for c in "⟨【["):
            return args
        from hermescloak.restorer import restore_json
        eng = _engine_for_session(None)
        vault = _scoped(eng.vault, _scope(session_id), "tool_request")
        out = restore_json(args, vault, "strict" if eng.profile.tolerant_restore else False)
        _report_foreign(vault, "tool_request")
        return out
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
        vault = _scoped(_engine_for(agent).vault, _scope(getattr(agent, "session_id", None)), "stream")
        out = restore_text(emit, vault)
        _report_foreign(vault, "stream")
        return out
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
        if _mode() != "enforce":
            return held
        vault = _scoped(_engine_for(agent).vault, _scope(getattr(agent, "session_id", None)), "stream")
        return restore_text(held, vault)
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
