#!/usr/bin/env python3
"""Enable / verify HermesCloak in hermes-agent.

HermesCloak used to be wired by inserting three source "seams" into the hermes-agent checkout,
which every hermes update overwrote (and which current hermes-agent no longer even has anchors
for). Current hermes-agent has a plugin system, so HermesCloak now ships as a plugin — a
directory plugin (``plugin.yaml`` + ``__init__.py`` at the repository root, installed with
``hermes plugins install aviv27-dev/HermesCloak --enable``), with the pip entry point
``hermes_agent.plugins: hermescloak`` kept for pip-based setups — and this script no longer edits
any hermes file. The command line is unchanged, so existing systemd
``ExecStartPre`` lines and git ``post-merge`` hooks keep working.

Usage:
  python install/apply_hooks.py --apply  [--hermes-root PATH]   # enable the plugin (idempotent)
  python install/apply_hooks.py --verify [--hermes-root PATH]   # exit 1 if not protected
  python install/apply_hooks.py --print                         # show what --apply does

--apply   runs ``hermes plugins enable hermescloak`` when the ``hermes`` CLI is on PATH, else adds
          ``hermescloak`` to ``plugins.enabled`` in ``$HERMES_HOME/config.yaml`` itself.
--verify  checks (1) the plugin is enabled (and not disabled) in config.yaml, (1b) that the runtime
          hermes actually runs (its PM-managed environment, when there is one) can import it and
          that plugins.isolation is not ``host``, (2) MODE, and
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


def _yaml():
    """PyYAML, or hermes' own YAML module inside a stock hermes runtime (which has no PyYAML)."""
    try:
        import yaml
    except ImportError:
        import hermes_yaml as yaml
    return yaml


def _config_path():
    return os.path.join(hermes_home(), "config.yaml")


def _load_config():
    p = _config_path()
    if not os.path.exists(p):
        return {}
    yaml = _yaml()
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
    yaml = _yaml()
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


INCIDENTS = ("plugin_load_failed", "seam_missing", "unfiltered_sent", "blocked_send", "leftover_token", "restore_error",
             "foreign_token", "restore_unscoped", "egress_blocked", "ner_auth_failed",
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
    """The latest enforce_send event's counts: ``in_request`` / ``in_system_prompt`` (that turn)
    and ``entities`` (cumulative distinct values in the vault)."""
    path = os.path.join(hermes_home(), "cloak", "audit.log")
    last = None
    try:
        for line in open(path, encoding="utf-8"):
            if '"enforce_send"' in line:
                last = line
        detail = json.loads(json.loads(last)["detail"]) if last else {}
        return detail if isinstance(detail, dict) else {}
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


def _managed_runtimes():
    """Python interpreters of hermes' PM-managed runtime(s) for this home, newest first.

    Current hermes-agent runs every CLI / gateway process from its own environment under
    ``$HERMES_HOME/installs/<install>/environments/<generation>/venv`` — NOT from the venv it was
    pip-installed into — so a plugin package must be visible THERE. (Older hermes ran from its venv.)"""
    pats = [os.path.join(hermes_home(), "installs", "*", "environments", "*", "venv", "bin", "python"),
            os.path.join(hermes_home(), "installs", "*", "environments", "*", "venv", "Scripts", "python.exe")]
    found = [p for pat in pats for p in glob.glob(pat)]
    return sorted(found, key=lambda p: os.path.getmtime(os.path.dirname(os.path.dirname(p))), reverse=True)


_PROBE_DIR_PLUGIN = (
    "import importlib.util, sys\n"
    "p = sys.argv[1]\n"
    "spec = importlib.util.spec_from_file_location('hermescloak_verify_probe', p + '/__init__.py',"
    " submodule_search_locations=[p])\n"
    "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); m._impl()\n")


def _probe(python, plugin_dir=None):
    """True if that interpreter can load the plugin the way hermes will (isolated, neutral cwd): through
    the directory plugin's own loader when there is one, else as an installed package."""
    if plugin_dir:
        cmd = [python, "-I", "-c", _PROBE_DIR_PLUGIN, plugin_dir]
    else:
        cmd = [python, "-I", "-c", "import hermescloak.hermes_plugin"]
    try:
        r = subprocess.run(cmd, cwd=os.path.expanduser("~"), capture_output=True, text=True, timeout=60)
        return r.returncode == 0, (r.stderr.strip().splitlines() or [""])[-1][:160]
    except Exception as exc:  # noqa: BLE001
        return False, repr(exc)[:160]


def _check_runtime(cfg):
    """Is the plugin importable by the process hermes will actually run?"""
    ok = True
    plugin_dir = os.path.join(hermes_home(), "plugins", PLUGIN)
    as_dir = os.path.isfile(os.path.join(plugin_dir, "plugin.yaml"))
    print(f"  [info] installed as: " + ("directory plugin (" + plugin_dir + ")" if as_dir
                                         else "pip package (entry point) — no directory plugin"))
    runtimes = _managed_runtimes()
    if runtimes:
        good, why = _probe(runtimes[0], plugin_dir if as_dir else None)
        print(f"  [{'OK ' if good else 'MISSING'}] hermescloak loads in hermes' managed runtime "
              f"({os.path.dirname(os.path.dirname(os.path.dirname(runtimes[0])))})"
              + ("" if good else f" — {why}. Install it the hermes way: "
                 "`hermes plugins install aviv27-dev/HermesCloak --enable` (a pip install into another "
                 "venv is NOT seen by this runtime)"))
        ok &= good
        env = os.path.dirname(os.path.dirname(os.path.dirname(runtimes[0])))
        sp = glob.glob(os.path.join(env, "venv", "lib", "python*", "site-packages")) or \
            glob.glob(os.path.join(env, "venv", "Lib", "site-packages"))
        inside = sorted({n for d in sp for n in os.listdir(d) if "hermescloak" in n.lower()})
        if inside:
            print(f"  [WARN] the SHARED runtime ({os.path.basename(env)}) contains {', '.join(inside)} — left by a "
                  "0.2.0 install (pip or package manager). Inert for this plugin, which loads its own copy, but "
                  "every agent's runtime carries it: `hermes pm install` publishes a runtime without it")
        else:
            print(f"  [OK ] shared runtime {os.path.basename(env)} contains no hermescloak package")
    else:
        try:
            import hermescloak.hermes_plugin  # noqa: F401
            print("  [OK ] hermescloak importable by this python (no PM-managed runtime in this home)")
        except Exception as exc:
            print(f"  [MISSING] hermescloak not importable by this python ({exc!r}) — "
                  "`hermes plugins install aviv27-dev/HermesCloak --enable`, or pip install it into hermes' venv")
            ok = False
    iso = str(((cfg.get("plugins") or {}) if isinstance(cfg.get("plugins"), dict) else {})
              .get("isolation") or "in_process").strip().lower()
    if iso == "host":
        print("  [WARN] plugins.isolation: host — third-party plugins run in a separate host process, so "
              "HermesCloak's inbound / streaming / auxiliary wraps cannot reach the agent. Set "
              "`plugins.isolation: in_process` (the default) for a protected agent.")
        ok = False
    return ok


def verify(root, strict=False):
    ok = True
    print(f"HERMES_HOME: {hermes_home()}\n--- HermesCloak verification ---")
    cfg = _load_config()
    en, dis = _plugin_lists(cfg)
    enabled = bool(en) and PLUGIN in en and PLUGIN not in dis
    print(f"  [{'OK ' if enabled else 'MISSING'}] plugin enabled in config.yaml (plugins.enabled)")
    ok &= enabled
    runtime = str(((cfg.get("model") or {}) if isinstance(cfg.get("model"), dict) else {})
                  .get("openai_runtime") or "auto").strip().lower()
    if runtime == "codex_app_server":
        print("  [WARN] model.openai_runtime: codex_app_server — turns are handed to the codex subprocess, "
              "which talks to OpenAI directly: HermesCloak does NOT cover that path. "
              "Switch it off (`/codex-runtime auto`) for a protected agent.")
        if strict:
            ok = False
    ok &= _check_runtime(cfg)
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
    last = last_masked_summary()

    def _fmt(d):
        return ", ".join(f"{k}: {v}" for k, v in sorted(d.items())) or "none"
    if "in_request" in last:
        print("  [info] last turn sent as tokens: " + _fmt(last["in_request"])
              + "  (of which in the system prompt: " + _fmt(last.get("in_system_prompt") or {}) + ")")
    if last.get("entities"):
        print("  [info] vault total, all turns so far (distinct values): " + _fmt(last["entities"]))
    try:
        from hermescloak.adapter.hermes_live import _load_profile, _cloak_dir
        from hermescloak.decide import shared as _decider
        prof = _load_profile(hermes_home(), _cloak_dir())
        if getattr(prof, "jev_check", False):
            d = _decider()
            b = getattr(prof, "decide_backend", "jev")
            where = d.local_url() if b == "local" else "OpenRouter"
            print(f"  [info] decision check: on — backend {b} ({where}) "
                  + ("configured" if d.configured(b) else "NOT configured (no key / url); the check is skipped")
                  + (" · shadow: " + ("local" if b == "jev" else "jev") if getattr(prof, "decide_shadow", False) else ""))
    except Exception:
        pass
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
