#!/usr/bin/env python3
"""Install/verify/remove the HermesCloak auto-loader (.pth).

Two patches must run in EVERY python process an agent spawns:

  * ``requests_egress`` — restore ⟦tokens⟧ in outbound HTTP bodies, including the
    ad-hoc scripts a model writes to send mail / call external APIs. There is no
    single mailer to wrap.
  * ``httpx_llm`` — mask LLM traffic at the transport layer. hermes reaches cloud
    models from ~120 auxiliary call sites that no source seam covers; patching httpx
    (used by the OpenAI SDK, the Anthropic SDK and the native Gemini adapter) covers
    all of them at once, including sites added by future hermes versions.

So we drop a `.pth` into site-packages: Python executes its `import` line at
interpreter startup — but ONLY when HERMES_HOME is set, so non-agent python on the
same box is untouched. Fail-open; remove the file to disable.

Note: this imports httpx at interpreter startup in an agent context, which costs a
few tens of ms per process. That is the price of catching the SDKs before first use.

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


def _resolve_home() -> str:
    return os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")


def _pth_content(home: str) -> str:
    """Build the .pth with the agent's HERMES_HOME baked in (cross-OS: the ~/.hermes
    fallback is wrong on Windows, so we hardcode the path resolved at install time).
    One physical 'import' line; site.py exec()s it; fully wrapped so it can't break startup."""
    cloak = os.path.join(home, "cloak")
    return (
        "# HermesCloak egress auto-loader: restore leaked tokens in outbound HTTP. Activates in an\n"
        "# agent context (HERMES_HOME set OR the baked cloak dir exists). Fail-open; delete to disable.\n"
        "import os; exec(\"try:\\n"
        " _hm = os.environ.get('HERMES_HOME') or {home!r}\\n"
        " if os.environ.get('HERMES_HOME') or os.path.isdir({cloak!r}):\\n"
        "  import hermescloak.integrations.requests_egress as _h; _h.install(_hm)\\n"
        "  import hermescloak.integrations.httpx_llm as _x; _x.install(_hm)\\n"
        "except Exception: pass\")\n"
    ).format(home=home, cloak=cloak)


def _target_dir() -> str:
    try:
        return site.getsitepackages()[0]
    except Exception:
        return site.getusersitepackages()


def _path() -> str:
    return os.path.join(_target_dir(), PTH_NAME)


def apply() -> int:
    p = _path()
    home = _resolve_home()
    with open(p, "w", encoding="utf-8") as f:
        f.write(_pth_content(home))
    print(f"[egress] wrote {p} (HERMES_HOME baked: {home})")
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
    # prove it actually patches in a fresh interpreter with HERMES_HOME set.
    # cwd is neutralised: `python -c` puts the current directory on sys.path, so probing
    # from a hermescloak checkout would import the source tree and report a green that a
    # real agent process (different cwd) would never see.
    import tempfile
    env = dict(os.environ, HERMES_HOME=os.environ.get("HERMES_HOME", "/tmp/_hce_probe"))
    code = ("import sys;"
            "import requests;"
            "eg = getattr(requests.sessions.Session.request,'__hermescloak_egress__',False);"
            "import httpx;"
            "lm = getattr(httpx.Client.send,'__hermescloak_llm__',False);"
            "print('egress=%s llm=%s' % (bool(eg), bool(lm)));"
            "sys.exit(0 if (eg and lm) else 3)")
    r = subprocess.run([sys.executable, "-c", code], env=env, cwd=tempfile.gettempdir(),
                       capture_output=True)
    out = r.stdout.decode("utf-8", "replace").strip()
    if r.returncode == 0:
        print(f"[egress] OK — installed and patching ({p})  [{out}]")
        return 0
    print("[egress] FILE present but the patches did NOT fully load "
          f"[{out or r.stderr.decode('utf-8', 'replace').strip()}]")
    print("         → check that hermescloak (and httpx) import in this interpreter")
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
