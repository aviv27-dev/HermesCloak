#!/usr/bin/env python3
"""Enable / verify HermesCloak in hermes-agent.

HermesCloak used to be wired by inserting three source "seams" into the hermes-agent checkout,
which every hermes update overwrote (and which current hermes-agent no longer even has anchors
for). Current hermes-agent has a plugin system, so HermesCloak now ships as a plugin
(``hermescloak.hermes_plugin``, pip entry point ``hermes_agent.plugins: hermescloak``) and this
script no longer edits any hermes file. The command line is unchanged, so existing systemd
``ExecStartPre`` lines and git ``post-merge`` hooks keep working.

Usage:
  python install/apply_hooks.py --apply  [--hermes-root PATH]   # enable the plugin (idempotent)
  python install/apply_hooks.py --verify [--hermes-root PATH]   # exit 1 if not protected
  python install/apply_hooks.py --print                         # show what --apply does

--apply   runs ``hermes plugins enable hermescloak`` when the ``hermes`` CLI is on PATH, else adds
          ``hermescloak`` to ``plugins.enabled`` in ``$HERMES_HOME/config.yaml`` itself.
--verify  checks (1) the plugin is enabled (and not disabled) in config.yaml, (2) MODE, and
          (3) with --hermes-root: that every hermes interception point the plugin needs still
          exists in that checkout — run it after each hermes update; a refactor there is reported
          as MISSING instead of silently reducing coverage.
"""
import argparse
import glob
import json
import os
import re
import time
import shutil
import subprocess
import sys

PLUGIN = "hermescloak"

# (description, file relative to the hermes root, regex that must match) — the targets
# hermescloak/hermes_plugin depends on.
TARGETS = [
    ("outbound: llm_request middleware is applied",
     "agent/turn_api_request.py", r"apply_llm_request_middleware\("),
    ("outbound: middleware API",
     "hermes_cli/middleware.py", r'LLM_REQUEST_MIDDLEWARE\s*=\s*"llm_request"'),
    ("inbound: transport registry",
     "agent/transports/__init__.py", r"def register_transport\(|_REGISTRY"),
    ("inbound: NormalizedResponse",
     "agent/transports/types.py", r"class NormalizedResponse"),
    ("streaming: _fire_stream_delta",
     "agent/stream_delivery.py", r"def _fire_stream_delta\("),
    ("streaming: _emit_stream_end",
     "agent/stream_delivery.py", r"def _emit_stream_end\("),
    ("auxiliary: sync completion funnel",
     "agent/auxiliary_client.py", r"def _relay_sync_completion\("),
    ("auxiliary: async completion funnel",
     "agent/auxiliary_client.py", r"async def _relay_async_completion\("),
]


def hermes_home():
    return os.path.realpath(os.path.expanduser(os.environ.get("HERMES_HOME") or "~/.hermes"))


def hermes_root(cli):
    if cli:
        return cli
    return (os.environ.get("HERMES_AGENT_ROOT")
            or (os.path.join(os.environ["HERMES_HOME"], "hermes-agent") if os.environ.get("HERMES_HOME") else None)
            or os.path.join(os.getcwd(), "hermes-agent"))


def _config_path():
    return os.path.join(hermes_home(), "config.yaml")


def _load_config():
    p = _config_path()
    if not os.path.exists(p):
        return {}
    import yaml
    with open(p, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _plugin_lists(cfg):
    plugins = cfg.get("plugins") or {}
    en = plugins.get("enabled")
    dis = plugins.get("disabled")
    return (en if isinstance(en, list) else None), (dis if isinstance(dis, list) else [])


def _enable_in_config():
    """Fallback when the hermes CLI is unavailable: add to plugins.enabled (keeps other keys;
    note PyYAML drops comments — prefer `hermes plugins enable`)."""
    import yaml
    cfg = _load_config()
    plugins = cfg.setdefault("plugins", {}) or {}
    cfg["plugins"] = plugins
    enabled = plugins.get("enabled") if isinstance(plugins.get("enabled"), list) else []
    if PLUGIN not in enabled:
        enabled.append(PLUGIN)
    plugins["enabled"] = enabled
    if isinstance(plugins.get("disabled"), list) and PLUGIN in plugins["disabled"]:
        plugins["disabled"].remove(PLUGIN)
    os.makedirs(hermes_home(), exist_ok=True)
    with open(_config_path(), "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)


def apply(dry):
    en, dis = _plugin_lists(_load_config())
    if en and PLUGIN in en and PLUGIN not in dis:
        print(f"  = already enabled in {_config_path()}")
        return 0
    hermes = shutil.which("hermes")
    if dry:
        print(f"  [dry-run] would run `hermes plugins enable {PLUGIN}`" if hermes
              else f"  [dry-run] would add {PLUGIN} to plugins.enabled in {_config_path()}")
        return 0
    if hermes:
        r = subprocess.run([hermes, "plugins", "enable", PLUGIN], stdin=subprocess.DEVNULL)
        if r.returncode == 0:
            print(f"  ✓ enabled via `hermes plugins enable {PLUGIN}`")
            return 0
        print(f"  ! `hermes plugins enable` failed (exit {r.returncode}); editing config.yaml directly")
    _enable_in_config()
    print(f"  ✓ added {PLUGIN} to plugins.enabled in {_config_path()}")
    print("  Restart the gateway so the agent process loads the plugin.")
    return 0


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


INCIDENTS = ("seam_missing", "unfiltered_sent", "blocked_send", "leftover_token", "restore_error",
             "leaked_original", "config_error", "vault_locked", "vault_key_invalid", "vault_key_unreadable",
             "vault_crypto_unavailable", "vault_write_failed", "vault_corrupt_quarantined",
             "vault_lock_timeout", "vault_error", "ner_down")
INFO = ("new_pii", "backstop_restore", "vault_restored_from_backup", "vault_journal_lines_skipped")


def _ts(rec):
    try:
        return time.mktime(time.strptime(rec["ts"][:19], "%Y-%m-%dT%H:%M:%S"))
    except Exception:
        return None


def audit_summary(hours=24.0):
    """(last plugin_active record, {kind: count} within `hours`) from the audit log (+ rotations)."""
    cloak = os.path.join(hermes_home(), "cloak")
    files = sorted(glob.glob(os.path.join(cloak, "audit.log*")), reverse=True)
    last_active, counts = None, {}
    cutoff = time.time() - hours * 3600
    for f in files:
        try:
            lines = open(f, encoding="utf-8").read().splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("kind") == "plugin_active":
                last_active = rec
            t = _ts(rec)
            if t is not None and t >= cutoff:
                counts[rec.get("kind")] = counts.get(rec.get("kind"), 0) + 1
    return last_active, counts


def last_masked_summary():
    """Entity-type counts from the latest enforce_send event (distinct values masked so far)."""
    path = os.path.join(hermes_home(), "cloak", "audit.log")
    last = None
    try:
        for line in open(path, encoding="utf-8"):
            if '"enforce_send"' in line:
                last = line
        detail = json.loads(json.loads(last)["detail"]) if last else {}
        return detail.get("entities") or {}
    except Exception:
        return {}


def vault_status():
    out = []
    try:
        from hermescloak.durable_vault import MAGIC, read_journal, read_payload
        from hermescloak.adapter.hermes_live import _vault_key
    except Exception as exc:
        return [f"cannot inspect vault ({exc!r})"]
    os.environ.setdefault("HERMES_HOME", hermes_home())
    key = _vault_key()
    for f in sorted(glob.glob(os.path.join(hermes_home(), "cloak", "vaults", "*.json"))):
        enc = open(f, "rb").read(len(MAGIC)) == MAGIC
        try:
            n = len(read_payload(f, key).get("token_to_real") or {})
            j = len(read_journal(f + ".journal", key)[0])
            out.append(f"{os.path.basename(f)}: {n} snapshot + {j} journal entries, "
                       f"{'ENCRYPTED' if enc else 'plaintext (0600)'}")
        except Exception as exc:
            out.append(f"{os.path.basename(f)}: UNREADABLE ({type(exc).__name__}) "
                       f"{'— encrypted, key not available here' if enc else ''}")
    return out or ["no vault yet (created on the first tokenized value)"]


def verify(root, strict=False):
    ok = True
    print(f"HERMES_HOME: {hermes_home()}\n--- HermesCloak verification ---")
    en, dis = _plugin_lists(_load_config())
    enabled = bool(en) and PLUGIN in en and PLUGIN not in dis
    print(f"  [{'OK ' if enabled else 'MISSING'}] plugin enabled in config.yaml (plugins.enabled)")
    ok &= enabled
    try:
        import hermescloak.hermes_plugin  # noqa: F401
        print("  [OK ] hermescloak importable by this python")
    except Exception as exc:
        print(f"  [MISSING] hermescloak not importable by this python ({exc!r}) — "
              "pip install it into the hermes venv")
        ok = False
    try:
        mode = open(os.path.join(hermes_home(), "cloak", "MODE"), encoding="utf-8").read().strip()
    except OSError:
        mode = "off (no cloak/MODE)"
    print(f"  [info] MODE = {mode}")
    if root and os.path.isdir(root):
        print(f"--- interception points in {root} ---")
        for desc, rel, pat in TARGETS:
            p = os.path.join(root, rel)
            present = os.path.exists(p) and re.search(pat, _read(p)) is not None
            print(f"  [{'OK ' if present else 'MISSING'}] {desc}  ({rel})")
            ok &= present
        legacy = [rel for rel in ("agent/chat_completion_helpers.py", "agent/conversation_loop.py",
                                  "gateway/run.py")
                  if os.path.exists(os.path.join(root, rel)) and "HermesCloak:" in _read(os.path.join(root, rel))]
        if legacy:
            print(f"  [info] legacy source seams still present in {legacy} — harmless "
                  "(restore is idempotent) and removed by the next hermes update")
    elif root:
        print(f"  [info] hermes root {root} not found — skipped the interception-point check")
    print("--- vault ---")
    for line in vault_status():
        print(f"  {line}")
    last, counts = audit_summary()
    print("--- audit (last 24h) ---")
    if last is None:
        print("  [info] no plugin_active yet — restart the gateway after enabling, then run a turn")
    else:
        try:
            patches = json.loads(last.get("detail") or "{}").get("patches", {})
        except ValueError:
            patches = {}
        bad = {k: v for k, v in patches.items() if not str(v).startswith("ok")}
        print(f"  [{'OK ' if not bad else 'MISSING'}] last plugin load {last.get('ts', '?')}: "
              + ("all interception points + self-test ok" if not bad else json.dumps(bad)))
        ok &= not bad
    if counts:
        print("  [info] events: " + ", ".join(f"{k}×{v}" for k, v in sorted(counts.items())))
    masked = last_masked_summary()
    if masked:
        print("  [info] masked by type (distinct values): " + ", ".join(f"{k}: {v}" for k, v in sorted(masked.items())))
    inc = {k: counts[k] for k in INCIDENTS if counts.get(k)}
    for k in INFO:
        if counts.get(k):
            print(f"  [info] {k}: {counts[k]}")
    for k, n in inc.items():
        print(f"  [WARN] {k}: {n}")
    if strict and inc:
        ok = False
    print("--- " + ("protected ✓ (confirm on a live turn: plugin_active + enforce_send in "
                    "$HERMES_HOME/cloak/audit.log)" if ok else "NOT PROTECTED — see MISSING above") + " ---")
    return ok


def main():
    ap = argparse.ArgumentParser(description="Enable/verify the HermesCloak hermes plugin")
    ap.add_argument("--hermes-root", default=None)
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--print", dest="show", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--strict", action="store_true", help="also fail on audit incidents in the last 24h")
    a = ap.parse_args()
    if a.show:
        print(__doc__)
        return 0
    if a.apply:
        return apply(a.dry_run)
    return 0 if verify(hermes_root(a.hermes_root), a.strict) else 1


if __name__ == "__main__":
    sys.exit(main())
