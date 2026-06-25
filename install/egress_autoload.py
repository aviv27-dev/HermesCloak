#!/usr/bin/env python3
"""Install/verify/remove the egress auto-loader (.pth).

The egress HTTP patch must run in EVERY python process an agent spawns — including
the ad-hoc scripts a model writes to send mail / call external APIs. There is no
single mailer to wrap, so we drop a `.pth` into site-packages: Python executes its
`import` line at interpreter startup, which installs the requests patch
(restore ⟦tokens⟧ in outbound HTTP bodies) — but ONLY when HERMES_HOME is set, so
non-agent python on the same box is untouched. Fail-open; remove the file to disable.

Usage:
  python -m install.egress_autoload --apply     # write the .pth into the current interpreter's site-packages
  python -m install.egress_autoload --verify    # check it is present and actually patches
  python -m install.egress_autoload --remove    # delete it

Run it with the SAME python the agent's scripts use (e.g. the system python3).
"""
import argparse
import os
import site
import subprocess
import sys

PTH_NAME = "hermescloak_egress.pth"
# One physical line. site.py exec()s lines beginning with "import". The exec body is
# guarded by HERMES_HOME and wrapped so a failure can never break interpreter startup.
PTH_LINE = (
    '# HermesCloak egress auto-loader: restore leaked tokens in outbound HTTP. Activates only in an\n'
    '# agent context (HERMES_HOME set OR ~/.hermes/cloak exists). Fail-open; delete this file to disable.\n'
    'import os; exec("try:\\n if os.environ.get(\'HERMES_HOME\') or os.path.isdir(os.path.expanduser(\'~/.hermes/cloak\')):\\n'
    '  import hermescloak.integrations.requests_egress as _h; _h.install()\\nexcept Exception: pass")\n'
)


def _target_dir() -> str:
    try:
        return site.getsitepackages()[0]
    except Exception:
        return site.getusersitepackages()


def _path() -> str:
    return os.path.join(_target_dir(), PTH_NAME)


def apply() -> int:
    p = _path()
    with open(p, "w", encoding="utf-8") as f:
        f.write(PTH_LINE)
    print(f"[egress] wrote {p}")
    return verify()


def remove() -> int:
    p = _path()
    if os.path.exists(p):
        os.remove(p)
        print(f"[egress] removed {p}")
    else:
        print(f"[egress] nothing to remove at {p}")
    return 0


def verify() -> int:
    p = _path()
    if not os.path.exists(p):
        print(f"[egress] NOT installed ({p} missing)")
        return 1
    # prove it actually patches in a fresh interpreter with HERMES_HOME set
    env = dict(os.environ, HERMES_HOME=os.environ.get("HERMES_HOME", "/tmp/_hce_probe"))
    code = ("import requests,sys;"
            "sys.exit(0 if getattr(requests.sessions.Session.request,'__hermescloak_egress__',False) else 3)")
    r = subprocess.run([sys.executable, "-c", code], env=env)
    if r.returncode == 0:
        print(f"[egress] OK — installed and patching ({p})")
        return 0
    print(f"[egress] FILE present but patch did NOT load — check that hermescloak imports in this interpreter")
    return 3


def main(argv) -> int:
    ap = argparse.ArgumentParser(description="HermesCloak egress auto-loader (.pth) installer")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--apply", action="store_true")
    g.add_argument("--verify", action="store_true")
    g.add_argument("--remove", action="store_true")
    a = ap.parse_args(argv)
    if a.apply:
        return apply()
    if a.remove:
        return remove()
    return verify()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
