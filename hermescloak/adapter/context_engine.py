"""CloakContextEngine — outbound masking through hermes's SUPPORTED plugin API.

Architecture v2. The v1 design patched three source seams into hermes-agent files;
every hermes update rewrote those files, the seams had to be re-applied (or carried as
a rebased local commit), and 0.20.0 actually broke seam C's anchor. This module
replaces the seams with ``ContextEngine.select_context()`` — a documented hook that:

  * runs every turn, before dispatch and before every request sanitizer,
  * may REPLACE the request message list for that single provider call,
  * never touches the persisted transcript (the host passes defensive copies),
  * fails open on any exception, and re-runs on retries.

That is exactly the outbound-masking contract, provided by upstream with tests.
A documented ABC survives refactors that move code around; an anchor line does not.

There is deliberately NO inbound restore. The model works in token-space and its
replies keep the ⟦tokens⟧; real values are substituted only at true egress (an actual
outbound action — see ``hermescloak.egress`` / the transport hook). Dropping
restore-on-return is what removes the other two seams, the streaming buffer, and the
need for a rehydration pass on every reply.

Wiring (see install/apply_hooks.py --apply):
  * a thin shim at ``<hermes>/plugins/context_engine/cloak/__init__.py`` calls
    ``register()`` here — the shim is the only file inside the hermes checkout, it is
    untracked (survives ``git pull``), and contains no logic worth breaking;
  * ``config.yaml`` selects it with ``context.engine: cloak``;
  * MODE stays per-deployment in ``$HERMES_HOME/cloak/MODE`` — a profile without a
    cloak dir gets passthrough behaviour from the same engine.

Compression parity: hermes deep-copies plugin engines per agent and does NOT pass the
host ``compression:`` config to them (external engines own compaction policy). So this
engine subclasses the built-in ContextCompressor — inheriting the exact compression
behaviour — and reads the same ``compression:`` keys from config.yaml itself. The one
knob that does not carry over is the host-side codex autoraise, which upstream
explicitly never applies to plugin engines.
"""
import json
import os
from pathlib import Path

_CONFIG_TO_CTOR = {
    # config.yaml `compression:` key      -> ContextCompressor ctor kwarg
    "threshold": "threshold_percent",
    "target_ratio": "summary_target_ratio",
    "protect_first_n": "protect_first_n",
    "protect_last_n": "protect_last_n",
    "abort_on_summary_failure": "abort_on_summary_failure",
}


def _compression_kwargs() -> dict:
    """Mirror the built-in compressor's config-derived constructor args.

    Reads ``compression:`` from ``$HERMES_HOME/config.yaml``. Unknown/absent keys
    fall back to ContextCompressor's own defaults — same as a stock install.
    """
    try:
        import yaml
        home = os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))
        with open(Path(home) / "config.yaml", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        comp = data.get("compression") or {}
        return {ctor: comp[key] for key, ctor in _CONFIG_TO_CTOR.items() if key in comp}
    except Exception:
        return {}


def build_cloak_engine():
    """Construct the engine, or return None when hermes is not importable.

    hermes classes are imported HERE, at call time inside the hermes process —
    never at hermescloak import time — so the hermescloak package stays importable
    (tests, egress CLI, standalone use) without a hermes checkout on sys.path.
    """
    try:
        from agent.context_compressor import ContextCompressor
    except Exception:
        return None

    class CloakContextEngine(ContextCompressor):
        """Built-in compression + outbound masking via select_context()."""

        @property
        def name(self) -> str:
            return "cloak"

        def select_context(self, request_messages, *, conversation_messages=None,
                           incoming_message=None, budget_tokens=0):
            """Mask the per-request copy. Return None (= unchanged) unless enforcing.

            All cloak state (engine, vault, MODE) is module-level in hermes_live,
            keyed by HERMES_HOME — nothing lives on `self`, which keeps the host's
            per-agent deepcopy of this engine trivially safe.
            """
            try:
                from hermescloak.adapter.hermes_live import (
                    _audit, _engine_for_home, _mode)
                mode = _mode()
                if mode not in ("shadow", "enforce"):
                    return None
                eng = _engine_for_home()
                masked = eng.sanitize_outbound(request_messages)
                if mode == "shadow":
                    _audit("shadow_detect",
                           json.dumps(eng.vault.summary(), ensure_ascii=False))
                    return None                      # prove detection, change nothing
                blob = json.dumps(masked, ensure_ascii=False)
                _audit("enforce_send", json.dumps(
                    {"via": "select_context", "entities": eng.vault.summary(),
                     "real_values_in_outbound": eng.vault.count_present(blob)},
                    ensure_ascii=False))
                return masked
            except Exception:
                # Fail-open here matches the host contract (it would catch and fall
                # open anyway). fail_mode: closed is enforced at the transport hook,
                # whose frame we own — a raise from inside select_context can never
                # block the send, so pretending otherwise would be a false promise.
                return None

    return CloakContextEngine(model="", **_compression_kwargs())


def register(ctx) -> None:
    """Entry point for hermes's plugin-style loader (register(ctx) pattern)."""
    eng = build_cloak_engine()
    if eng is not None:
        ctx.register_context_engine(eng)
