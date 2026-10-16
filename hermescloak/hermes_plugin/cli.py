"""``hermes hermescloak …`` — the documented check commands, run inside hermes' own runtime.

  hermes hermescloak verify   static + install check of THIS hermes (exit 1 = not protected)
  hermes hermescloak test     end-to-end proof: a real agent turn against a local fake cloud, in a
                              throwaway home that is deleted afterwards — your agents are not touched

Both run the scripts shipped in the plugin directory with the interpreter hermes itself runs on, so
they test exactly what the gateway will load. Nothing here sends anything off the machine.
"""
from __future__ import annotations

import os
import subprocess
import sys


def _plugin_root() -> str | None:
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return root if os.path.isfile(os.path.join(root, "install", "apply_hooks.py")) else None


def _hermes_root() -> str | None:
    try:
        from hermes_cli._startup_fast import project_root_str
        return project_root_str()
    except Exception:
        pass
    try:
        import agent
        return os.path.dirname(os.path.dirname(os.path.abspath(agent.__file__)))
    except Exception:
        return None


def _run(script: str, *args: str) -> int:
    root = _plugin_root()
    if root is None:
        print("hermescloak: the install/ scripts are not in this plugin directory (pip-layout install). "
              "Clone https://github.com/aviv27-dev/HermesCloak and run them from there.")
        return 2
    return subprocess.call([sys.executable, os.path.join(root, "install", script), *args])


def setup(parser) -> None:
    sub = parser.add_subparsers(dest="hermescloak_cmd")
    v = sub.add_parser("verify", help="check that HermesCloak is installed, enabled and hooked into this hermes")
    v.add_argument("--strict", action="store_true", help="also fail on audited incidents (monitoring)")
    t = sub.add_parser("test", help="end-to-end proof against a local fake cloud (throwaway home, deleted after)")
    t.add_argument("--provider", choices=["openai", "anthropic", "both"], default="openai")


def handle(args) -> int:
    cmd = getattr(args, "hermescloak_cmd", None)
    hroot = _hermes_root()
    if cmd == "verify":
        extra = ["--strict"] if getattr(args, "strict", False) else []
        return _run("apply_hooks.py", "--verify", *extra, *(["--hermes-root", hroot] if hroot else []))
    if cmd == "test":
        return _run("e2e_check.py", "--provider", getattr(args, "provider", "openai"),
                    *(["--hermes-root", hroot] if hroot else []))
    print(__doc__)
    return 2


def register(ctx) -> str:
    fn = getattr(ctx, "register_cli_command", None)
    if not callable(fn):
        return "missing: ctx.register_cli_command"
    fn("hermescloak", "HermesCloak privacy plugin: verify / test", setup, handle,
       description="Check (verify) or prove end to end (test) that HermesCloak protects this hermes.")
    return "ok"
