"""Live integration shim for hermes-agent. The two live seams call these two functions.

Scoped + safe by design:
  * MODE from `$HERMES_HOME/cloak/MODE` ∈ {absent|off|shadow|enforce}. Absent/off → passthrough,
    so any agent without the cloak dir is unaffected (scopes the deployment to one agent).
  * shadow → run detection to PROVE it works (audit logs counts/types only, NEVER real PII) but
    SEND THE ORIGINAL outbound and restore nothing (zero behaviour change).
  * enforce → tokenize the outbound copy; restore the response (content + tool-call args) in place.
  * FAIL-OPEN: any exception → return the original + buffer an audit line; never raise into hermes.

Duck-typed on the passed objects (no hermes import) so it stays testable and import-safe.
The audit log never contains real PII — only event kinds, counts, and entity types."""
import json
import os
import threading
from pathlib import Path

from hermescloak.adapter.alerts import AlertEvent, FileAuditAlerter
from hermescloak.engine import Engine
from hermescloak.entities import StaticFileSource
from hermescloak.profile import Profile

_LOCK = threading.Lock()
_ENGINES: dict[str, Engine] = {}


def _home() -> str:
    return os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))


def _cloak_dir() -> Path:
    return Path(_home()) / "cloak"


_MODE_TTL = 5.0                 # seconds; a MODE flip takes effect within this window
_mode_cache: dict[str, tuple[float, str]] = {}


def _mode() -> str:
    """MODE for the current HERMES_HOME, cached briefly.

    This is called on EVERY outbound request, every restore, and every streaming
    delta — an uncached read meant one filesystem syscall per streamed token.
    """
    import time
    home = _home()
    hit = _mode_cache.get(home)
    now = time.monotonic()
    if hit is not None and (now - hit[0]) < _MODE_TTL:
        return hit[1]
    try:
        val = (_cloak_dir() / "MODE").read_text(encoding="utf-8").strip().lower()
    except Exception:
        val = "off"
    _mode_cache[home] = (now, val)
    return val


def _audit(kind: str, detail: str) -> None:
    try:
        FileAuditAlerter(str(_cloak_dir() / "audit.log")).send(AlertEvent(kind, _home(), detail))
    except Exception:
        pass


def fail_closed() -> bool:
    """Does this deployment want a masking failure to BLOCK the send?

    `fail_mode` was parsed into Profile from day one but read by nothing, so
    `fail_mode: closed` silently behaved as open — the operator believed they had a
    blocking guarantee they did not have. Reading it here makes the setting real.
    Defaults to open: a broken cloak must not take a working agent down unless the
    deployment explicitly asked for that trade.
    """
    try:
        prof_path = _cloak_dir() / "profile.yaml"
        if not prof_path.exists():
            return False
        return Profile.from_yaml(str(prof_path)).fail_mode.strip().lower() == "closed"
    except Exception:
        return False


def _build_engine(session_id: str = "default") -> Engine:
    d = _cloak_dir()
    prof_path = d / "profile.yaml"
    profile = Profile.from_yaml(str(prof_path)) if prof_path.exists() else Profile(name="default")
    gaz = d / "gazetteer.txt"
    source = StaticFileSource(str(gaz)) if gaz.exists() else None
    extra: list = []
    ner_url_file = d / "ner_url"
    if ner_url_file.exists():
        url = ner_url_file.read_text(encoding="utf-8").strip()
        if url:                               # guard: empty ner_url → no NER (don't wire a dead client)
            from hermescloak.adapter.ner_client import NerServiceRecognizer
            extra.append(NerServiceRecognizer(url, control_file=str(d / "ner.ctl")))
    return Engine(profile, entity_source=source, extra_recognizers=extra or None,
                  vault=_agent_vault())


# ONE durable vault PER AGENT (keyed by HERMES_HOME), shared across all of that agent's
# sessions and reloaded on restart. This is what guarantees no broken deliverables: every
# token the agent ever issued stays restorable across sessions AND gateway restarts, so a
# restart mid-task can never leave an unrestorable ⟦token⟧ in an output. Opt out with
# "memory" in $HERMES_HOME/cloak/vault_mode (instant rollback). Fail-open to in-memory.
_AGENT_VAULTS: dict = {}
_VLOCK = threading.Lock()      # separate from _LOCK: _engine_for holds _LOCK when it calls here


def _agent_vault():
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
                    vdir = d / "vaults"
                    DurableVault.sweep_expired(str(vdir))
                    v = DurableVault(DurableVault.path_for(str(vdir), home))  # per-AGENT, persistent
            except Exception:
                from hermescloak.vault import Vault
                v = Vault()                           # fail-open
            _AGENT_VAULTS[home] = v
        return v


def _engine_for(agent) -> Engine:
    sid = str(getattr(agent, "session_id", None) or "default")
    with _LOCK:
        eng = _ENGINES.get(sid)
        if eng is None:
            eng = _build_engine(sid)                  # shares the per-agent vault
            _ENGINES[sid] = eng
        return eng


_TRANSPORT_ENGINES: dict[str, Engine] = {}


def _engine_for_home() -> Engine:
    """The one Engine for this process's HERMES_HOME, regardless of MODE.

    Shared by every caller that has no `agent` object in scope: the context-engine
    plugin (select_context), the httpx transport hook, and shadow-mode probes. All of
    them share the per-agent durable vault, so a token minted anywhere restores at
    egress. Keyed by HERMES_HOME because one gateway process serves one profile.
    """
    home = _home()
    with _LOCK:
        eng = _TRANSPORT_ENGINES.get(home)
        if eng is None:
            eng = _build_engine("transport")
            _TRANSPORT_ENGINES[home] = eng
        return eng


def transport_engine():
    """Engine for the transport hook. Returns None unless MODE is enforce."""
    if _mode() != "enforce":
        return None
    return _engine_for_home()


def cloak_sanitize_outbound(agent, api_messages):
    mode = _mode()
    if mode not in ("shadow", "enforce"):
        return api_messages
    try:
        eng = _engine_for(agent)
        sanitized = eng.sanitize_outbound(api_messages)
        if mode == "shadow":
            _audit("shadow_detect", json.dumps(eng.vault.summary(), ensure_ascii=False))
            return api_messages  # zero behaviour change — original goes to the cloud
        # enforce: prove no detected real value reached the cloud-bound copy.
        # Serialize the WHOLE payload: the old version joined only str content, so it
        # could not see leaks in multimodal (list) content — it reported a clean 0 for
        # exactly the payloads that leaked most. The proof must cover every shape.
        blob = json.dumps(sanitized, ensure_ascii=False)
        _audit("enforce_send", json.dumps(
            {"entities": eng.vault.summary(), "real_values_in_outbound": eng.vault.count_present(blob)},
            ensure_ascii=False))
        return sanitized
    except Exception as exc:  # noqa: BLE001 — fail-open unless the profile says otherwise
        _audit("unfiltered_sent", repr(exc))
        if fail_closed():
            from hermescloak.errors import CloakFailClosed
            _audit("fail_closed_block", repr(exc))
            raise CloakFailClosed(f"masking failed, send blocked: {exc!r}") from exc
        return api_messages


# Architecture v2 has NO restore-on-return. ``cloak_restore_inbound``,
# ``cloak_restore_text_for`` and ``cloak_filter_stream_delta`` (seams B and C) were
# deleted here: the model works in token-space, replies keep their ⟦tokens⟧, and real
# values re-enter only at true egress (hermescloak.egress + the transport hook's
# non-chat restore path). A hermes checkout still carrying the old seams degrades
# cleanly — the seams' own try/except swallows the ImportError into a no-op.
# ``cloak_sanitize_outbound`` above is kept callable for the same transitional
# reason: a leftover seam A just re-masks already-masked text, which is idempotent.
