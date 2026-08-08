#!/usr/bin/env python3
"""Install / verify HermesCloak architecture v2 in a hermes-agent checkout.

v1 wired the cloak by inserting three source seams into hermes-agent files; every
hermes update rewrote them (0.20.0 broke seam C's anchor outright) and a venv rebuild
could silently disarm the package behind a green check. v2 has NO source seams:

  A. **Context-engine plugin** — a thin shim at
     ``<hermes>/plugins/context_engine/cloak/__init__.py`` registers
     ``CloakContextEngine`` (hermescloak.adapter.context_engine), which masks the
     main loop through the supported ``select_context()`` hook. Selected by
     ``context.engine: cloak`` in config.yaml. The shim is untracked, logic-free,
     and survives ``git pull``.
  B. **httpx transport hook** — loaded at interpreter startup via the ``.pth``
     autoloader (install/egress_autoload.py): masks auxiliary LLM traffic, restores
     ⟦tokens⟧ in outbound ACTION bodies. There is no restore-on-return anywhere.

Usage:
  python install/apply_hooks.py --apply   --hermes-root <ROOT>   # install shim + .pth
  python install/apply_hooks.py --verify  --hermes-root <ROOT>   # doctor: full check
  python install/apply_hooks.py --remove  --hermes-root <ROOT>   # remove the shim

Default hermes root: $HERMES_AGENT_ROOT, else $HERMES_HOME/hermes-agent, else ./hermes-agent.

``--verify`` is the doctor. It proves each link of the actual runtime chain — not
just file presence — and exits non-zero when the cloak cannot be live:
  1. hermescloak importable by the venv interpreter that RUNS the agent
     (probed from a neutral cwd: `python -c` puts the cwd on sys.path, so probing
     from a hermescloak checkout would self-green);
  2. the cloak engine shim present and loadable;
  3. ``context.engine: cloak`` selected in $HERMES_HOME/config.yaml;
  4. the .pth autoloader present and actually patching httpx;
  5. leftover v1 seams reported (informational — they degrade to no-ops).
"""
import argparse
import os
import subprocess
import sys
import tempfile

SHIM_RELPATH = os.path.join("plugins", "context_engine", "cloak", "__init__.py")

SHIM = '''"""HermesCloak context-engine shim — the ONLY file inside the hermes checkout.

Logic-free on purpose: everything lives in the hermescloak package, so a hermes
update can at worst delete this file (it is untracked; `git pull` won't touch it),
and `apply_hooks.py --verify` catches that. Selected via `context.engine: cloak`.
"""


def register(ctx):
    try:
        from hermescloak.adapter.context_engine import register as _register
        _register(ctx)
    except Exception:
        pass          # hermescloak missing from the venv -> engine simply not offered
'''


def hermes_root(cli):
    if cli:
        return cli
    return (os.environ.get("HERMES_AGENT_ROOT")
            or (os.path.join(os.environ["HERMES_HOME"], "hermes-agent") if os.environ.get("HERMES_HOME") else None)
            or os.path.join(os.getcwd(), "hermes-agent"))


def _venv_python(root, override=None):
    """The interpreter that actually RUNS the agent (not the one running this script)."""
    if override:
        return override
    for rel in ("venv/bin/python", "venv/bin/python3", ".venv/bin/python",
                "venv/Scripts/python.exe"):
        p = os.path.join(root, rel)
        if os.path.exists(p):
            return p
    return None


def _hermes_home():
    return os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")


# ---------------------------------------------------------------- checks --

def check_importable(root, venv_python=None):
    """1. Can the agent's interpreter import hermescloak? None = unprovable."""
    py = _venv_python(root, venv_python)
    if py is None:
        print("  [WARN   ] no venv found under the hermes root — cannot prove importability")
        print("            pass --venv-python <path> to check the real interpreter")
        return None
    try:
        r = subprocess.run([py, "-c", "import hermescloak; print(hermescloak.__file__)"],
                           capture_output=True, timeout=30, cwd=tempfile.gettempdir())
    except Exception as exc:                 # noqa: BLE001 — report, never raise
        print(f"  [WARN   ] could not run {py}: {exc!r}")
        return None
    if r.returncode == 0:
        origin = r.stdout.decode("utf-8", "replace").strip()
        print(f"  [OK     ] hermescloak importable by {py}")
        print(f"            resolved from: {origin}")
        return True
    print(f"  [BROKEN ] hermescloak NOT importable by {py}")
    print("            → the cloak cannot load AT ALL in the agent process.")
    print(f"            → fix: {py} -m pip install -e <hermescloak checkout>")
    return False


def check_shim(root):
    """2. Engine shim present in plugins/context_engine/cloak/."""
    p = os.path.join(root, SHIM_RELPATH)
    if os.path.exists(p) and "hermescloak.adapter.context_engine" in open(p, encoding="utf-8").read():
        print(f"  [OK     ] engine shim present ({SHIM_RELPATH})")
        return True
    print(f"  [MISSING] engine shim not installed → run --apply")
    return False


def check_config_selects_cloak():
    """3. config.yaml actually selects the engine. Without this everything above is inert."""
    cfg = os.path.join(_hermes_home(), "config.yaml")
    try:
        with open(cfg, encoding="utf-8") as fh:
            text = fh.read()
    except Exception as exc:                 # noqa: BLE001
        print(f"  [WARN   ] could not read {cfg}: {exc!r}")
        return None
    try:
        import yaml
        data = yaml.safe_load(text) or {}
        engine = ((data.get("context") or {}).get("engine") or "compressor")
    except Exception:
        # this script must run with ANY python (system python has no yaml) — fall
        # back to a shape-aware scan for the two-line `context:\n  engine: X` block
        import re
        m = re.search(r"^context:\s*\n(?:[ \t]+\w+.*\n)*?[ \t]+engine:[ \t]*([\w-]+)",
                      text, re.MULTILINE)
        engine = m.group(1) if m else "compressor"
    if engine == "cloak":
        print(f"  [OK     ] context.engine: cloak selected ({cfg})")
        return True
    print(f"  [OFF    ] context.engine is '{engine}' (not 'cloak') in {cfg}")
    print("            → the engine is installed but NOT selected; main-loop masking is off.")
    print("            → set:\n                context:\n                  engine: cloak")
    return False


def check_pth(root, venv_python=None):
    """4. .pth autoloader present AND actually patching httpx in a fresh interpreter."""
    py = _venv_python(root, venv_python)
    if py is None:
        return None
    env = dict(os.environ, HERMES_HOME=_hermes_home())
    code = ("import sys, httpx;"
            "sys.exit(0 if getattr(httpx.Client.send,'__hermescloak_llm__',False) else 3)")
    try:
        r = subprocess.run([py, "-c", code], env=env, capture_output=True,
                           timeout=30, cwd=tempfile.gettempdir())
    except Exception as exc:                 # noqa: BLE001
        print(f"  [WARN   ] could not probe the autoloader: {exc!r}")
        return None
    if r.returncode == 0:
        print("  [OK     ] .pth autoloader live (httpx patched at interpreter startup)")
        return True
    print("  [MISSING] transport hook not loading at startup → run --apply")
    return False


def report_v1_seams(root):
    """5. Informational: v1 seams left in the source degrade to harmless no-ops."""
    remnants = []
    for rel, marker in (("agent/chat_completion_helpers.py", "cloak_sanitize_outbound"),
                        ("agent/conversation_loop.py", "cloak_restore_inbound"),
                        ("gateway/run.py", "cloak_filter_stream_delta")):
        p = os.path.join(root, rel)
        try:
            if marker in open(p, encoding="utf-8").read():
                remnants.append(rel)
        except Exception:
            continue
    if remnants:
        print(f"  [INFO   ] v1 seam remnants in: {', '.join(remnants)}")
        print("            harmless (their own try/except no-ops them; outbound seam")
        print("            re-masking is idempotent) — drop them from any carried patch"
              " at leisure.")


# ----------------------------------------------------------------- verbs --

def verify(root, venv_python=None):
    print(f"hermes-agent root: {root}\nHERMES_HOME:       {_hermes_home()}")
    print("--- HermesCloak v2 verification (no seams — engine + transport) ---")
    results = [check_importable(root, venv_python),
               check_shim(root),
               check_config_selects_cloak(),
               check_pth(root, venv_python)]
    report_v1_seams(root)
    hard_fail = any(r is False for r in results)
    unproven = any(r is None for r in results)
    if hard_fail:
        print("--- NOT PROTECTED — see above ---")
        return False
    if unproven:
        print("--- installed, but not fully provable from here ⚠ ---")
        return False
    print("--- all four links live ✓ ---")
    return True


def apply(root, venv_python=None):
    # a) engine shim
    p = os.path.join(root, SHIM_RELPATH)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(SHIM)
    print(f"  ✓ wrote {p}")
    # b) .pth autoloader, installed BY the venv interpreter so it lands in ITS site-packages
    py = _venv_python(root, venv_python)
    if py is None:
        print("  ⚠ no venv found — install the .pth manually:")
        print("      <venv-python> " + os.path.join(os.path.dirname(__file__), "egress_autoload.py") + " --apply")
    else:
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "egress_autoload.py")
        r = subprocess.run([py, script, "--apply"], env=dict(os.environ, HERMES_HOME=_hermes_home()))
        print(f"  {'✓' if r.returncode == 0 else '⚠'} egress_autoload --apply via {py} (exit {r.returncode})")
    # c) config is the operator's file — never rewritten here, only instructed
    if check_config_selects_cloak() is not True:
        print("  → finish by setting context.engine: cloak in config.yaml, then restart the gateway")
    else:
        print("  → restart the gateway to load the engine")


def remove(root):
    p = os.path.join(root, SHIM_RELPATH)
    if os.path.exists(p):
        os.remove(p)
        print(f"  ✓ removed {p}")
        try:
            os.rmdir(os.path.dirname(p))
        except OSError:
            pass
    else:
        print(f"  = nothing to remove at {p}")
    print("  → also set context.engine back to 'compressor' and (optionally) run"
          " egress_autoload --remove")


def main():
    ap = argparse.ArgumentParser(description="Install/verify HermesCloak v2 (engine + transport, no seams)")
    ap.add_argument("--hermes-root", default=None)
    ap.add_argument("--venv-python", default=None,
                    help="interpreter that runs the agent (default: <root>/venv/bin/python)")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--remove", action="store_true")
    a = ap.parse_args()
    root = hermes_root(a.hermes_root)
    if a.apply:
        apply(root, a.venv_python)
        return 0
    if a.remove:
        remove(root)
        return 0
    # default = verify
    return 0 if verify(root, a.venv_python) else 1


if __name__ == "__main__":
    sys.exit(main())
