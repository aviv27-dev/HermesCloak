#!/usr/bin/env python3
"""Install / verify the HermesCloak seams in a hermes-agent checkout.

hermes-agent has no plugin system, so HermesCloak is wired by inserting three tiny,
fail-open call-sites ("seams") into the agent source. A hermes-agent **version update
overwrites those files** — so after every update you re-run this to re-apply + verify.

Usage:
  python install/apply_hooks.py --verify                 # report which seams are present
  python install/apply_hooks.py --apply                  # insert missing seams (idempotent)
  python install/apply_hooks.py --print                  # print the blocks for manual paste
  python install/apply_hooks.py --apply --hermes-root /path/to/hermes-agent

Default hermes root: $HERMES_AGENT_ROOT, else $HERMES_HOME/hermes-agent, else ./hermes-agent.

Each seam is guarded by `try/except: pass`, so even a wrong/edited insertion can never
raise into the agent. `--verify` is the authoritative check (substring of a stable
sentinel). `--apply` is best-effort anchor insertion; if an anchor is not found (e.g. a
future hermes refactor moved it), it prints the block + location for a 30-second manual paste.
The streaming seam (C) wraps an existing callback, so it is verify+manual-print only.
"""
import argparse
import os
import sys

SENTINEL = "HermesCloak:"

# Seam A — OUTBOUND tokenize. Simple insert right after the build_api_kwargs docstring.
A_FILE = "agent/chat_completion_helpers.py"
A_ANCHOR = '    """Build the keyword arguments dict for the active API mode."""'
A_BLOCK = '''    try:  # HermesCloak: tokenize a COPY of outbound messages before the cloud (MODE-scoped, fail-open)
        from hermescloak.adapter.hermes_live import cloak_sanitize_outbound
        api_messages = cloak_sanitize_outbound(agent, api_messages)
    except Exception:
        pass'''

# Seam B — INBOUND restore. Insert after the transport-agnostic assistant-message convergence.
B_FILE = "agent/conversation_loop.py"
B_ANCHOR = "            assistant_message = normalized"
B_BLOCK = '''            try:  # HermesCloak: restore real values (content + tool-call args) — codex-safe convergence
                from hermescloak.adapter.hermes_live import cloak_restore_inbound
                assistant_message = cloak_restore_inbound(agent, assistant_message)
            except Exception:
                pass'''

# Seam C — STREAMING last-mile restore. Wraps stream_delta_callback; manual paste only.
C_FILE = "gateway/run.py"
C_BLOCK = '''            # HermesCloak: restore ⟦tokens⟧ in streamed deltas before they reach the gateway
            # consumer, so the user-facing reply shows real values (enforce-only, fail-open).
            if _stream_delta_cb is not None:
                try:
                    from hermescloak.adapter.hermes_live import cloak_filter_stream_delta as _cloak_sd
                    _cloak_sd_state = {}
                    def _cloak_stream_delta_cb(_t, __cb=_stream_delta_cb, __ag=agent, __st=_cloak_sd_state):
                        _out = _cloak_sd(__ag, __st, _t)
                        if _out:
                            __cb(_out)
                    agent.stream_delta_callback = _cloak_stream_delta_cb
                except Exception:
                    agent.stream_delta_callback = _stream_delta_cb'''

SEAMS = [
    {"name": "A outbound (chat_completion_helpers)", "file": A_FILE, "anchor": A_ANCHOR,
     "block": A_BLOCK, "auto": True, "after": True},
    {"name": "B inbound  (conversation_loop)", "file": B_FILE, "anchor": B_ANCHOR,
     "block": B_BLOCK, "auto": True, "after": True},
    {"name": "C stream   (gateway/run)", "file": C_FILE, "anchor": None,
     "block": C_BLOCK, "auto": False, "after": True},
]


def hermes_root(cli):
    if cli:
        return cli
    return (os.environ.get("HERMES_AGENT_ROOT")
            or (os.path.join(os.environ["HERMES_HOME"], "hermes-agent") if os.environ.get("HERMES_HOME") else None)
            or os.path.join(os.getcwd(), "hermes-agent"))


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def verify(root):
    print(f"hermes-agent root: {root}\n--- HermesCloak seam verification ---")
    ok = True
    for s in SEAMS:
        p = os.path.join(root, s["file"])
        present = os.path.exists(p) and SENTINEL in _read(p) and s["block"].split("\n")[0].strip()[:30] in _read(p)
        # robust check: sentinel + the seam-specific import symbol
        sym = "cloak_sanitize_outbound" if "A " in s["name"] else "cloak_restore_inbound" if "B " in s["name"] else "cloak_filter_stream_delta"
        present = os.path.exists(p) and sym in _read(p)
        print(f"  [{'OK ' if present else 'MISSING'}] {s['name']}  ({s['file']})")
        ok = ok and present
    print("--- " + ("all seams present ✓" if ok else "SOME SEAMS MISSING — run --apply or --print") + " ---")
    return ok


def apply(root, dry):
    changed = 0
    for s in SEAMS:
        p = os.path.join(root, s["file"])
        if not os.path.exists(p):
            print(f"  ! {s['file']} not found — skip {s['name']}")
            continue
        src = _read(p)
        sym = s["block"].split("import ")[-1].split(" as")[0].split("\n")[0].strip() if "import " in s["block"] else ""
        if any(t in src for t in ("cloak_sanitize_outbound", "cloak_restore_inbound", "cloak_filter_stream_delta")
               if t in s["block"]):
            print(f"  = already present: {s['name']}")
            continue
        if not s["auto"] or s["anchor"] is None or s["anchor"] not in src:
            print(f"  ⚠ manual insert needed: {s['name']}  (anchor not found or wrap-style)")
            print(f"     → paste this block into {s['file']}:\n")
            print("\n".join("       " + ln for ln in s["block"].splitlines()) + "\n")
            continue
        idx = src.index(s["anchor"]) + len(s["anchor"])
        new = src[:idx] + "\n" + s["block"] + src[idx:]
        if dry:
            print(f"  [dry-run] would insert seam {s['name']} after anchor in {s['file']}")
        else:
            with open(p, "w", encoding="utf-8") as f:
                f.write(new)
            print(f"  ✓ inserted: {s['name']}")
            changed += 1
    if not dry:
        print(f"\n{changed} seam(s) inserted. Run --verify to confirm, then restart the gateway.")


def main():
    ap = argparse.ArgumentParser(description="Install/verify HermesCloak seams in hermes-agent")
    ap.add_argument("--hermes-root", default=None)
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--print", dest="show", action="store_true", help="print all seam blocks")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    root = hermes_root(a.hermes_root)
    if a.show:
        for s in SEAMS:
            print(f"\n=== {s['name']} — {s['file']} ===")
            if s["anchor"]:
                print(f"# insert AFTER the line:\n#   {s['anchor'].strip()}")
            else:
                print("# wrap-style — see the existing stream_delta_callback assignment")
            print(s["block"])
        return 0
    if a.apply:
        apply(root, a.dry_run)
        return 0
    # default = verify
    return 0 if verify(root) else 1


if __name__ == "__main__":
    sys.exit(main())
