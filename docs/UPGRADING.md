# Surviving hermes-agent version updates

HermesCloak wires into hermes-agent by inserting three small seams into the agent source
(see [INTEGRATION.md](INTEGRATION.md)). An update can silently disarm the privacy layer in **two**
distinct ways, and they need different fixes:

1. **The update rewrites the seam files**, removing the seams. Fix: re-apply.
2. **The update rebuilds the agent's venv**, removing the installed `hermescloak` package while
   leaving the seam source text untouched. The seams are `try/except: pass`, so they degrade to
   silent no-ops: masking is completely off while a seams-only check reports a healthy green.
   Fix: reinstall the package into that venv.

Failure mode 2 is the nastier one — every visible indicator says healthy. It is why `--verify` now
also proves the package is importable *by the interpreter that runs the agent*, and exits non-zero
when it is not.

## The risk in one line

After `hermes-agent` updates, the privacy layer can be **silently gone**. Always re-verify.

## Procedure for every hermes-agent update

```bash
# 1. update hermes-agent as usual (the seams and/or the venv install are now likely gone)

# 2. re-verify — checks seams AND that the agent's interpreter can import hermescloak
python install/apply_hooks.py --verify --hermes-root $HERMES_HOME/hermes-agent

# 3a. if a seam is MISSING, re-apply (idempotent — skips seams already present)
python install/apply_hooks.py --apply --hermes-root $HERMES_HOME/hermes-agent
#    if an anchor moved in the new version, --apply prints the exact block to paste manually
#    (and `--print` shows all three blocks + their target locations)

# 3b. if it reports BROKEN (not importable), reinstall into the agent's venv:
$HERMES_HOME/hermes-agent/venv/bin/python -m pip install -e /path/to/HermesCloak
#    then re-run the .pth auto-loader so the transport hook loads at startup:
$HERMES_HOME/hermes-agent/venv/bin/python -m install.egress_autoload --apply

# 4. re-verify until seams show OK *and* the runtime check passes
python install/apply_hooks.py --verify --hermes-root $HERMES_HOME/hermes-agent

# 5. restart the gateway so the agent process loads the patched files
#    (this is the ONLY step that interrupts live sessions — schedule it)

# 6. confirm it is actually filtering, on a real turn:
tail $HERMES_HOME/cloak/audit.log
#    look for fresh enforce_send with "real_values_in_outbound": 0
```

**A stale `audit.log` mtime while gateways are serving is proof the cloak is off**, whatever
`--verify` says. It is the one signal that cannot be faked by the seams being textually present.

## Make it automatic (recommended)

Add the verify step to whatever you use to update hermes-agent, so a missing seam fails loudly
instead of silently:

```bash
hermes-agent-update.sh && \
  python /path/to/HermesCloak/install/apply_hooks.py --apply  --hermes-root $HERMES_HOME/hermes-agent && \
  python /path/to/HermesCloak/install/apply_hooks.py --verify --hermes-root $HERMES_HOME/hermes-agent || \
  echo "‼ HermesCloak seams missing after update — privacy layer is OFF until fixed"
```

**Best for a systemd-managed gateway (fully automatic, no reminders).** Add an `ExecStartPre` to the
gateway unit so the seams are re-applied *before every start* — and since a gateway restart always
follows an update, this restores the privacy layer with nobody having to remember:

```ini
# /etc/systemd/system/<gateway>.service.d/42-cloak-reapply.conf
[Service]
ExecStartPre=-/usr/bin/python3 /path/to/HermesCloak/install/apply_hooks.py --apply --hermes-root /path/to/hermes-agent
```

Then `systemctl daemon-reload`. The leading `-` makes it non-fatal (the gateway still starts if it
errors — fail-open, consistent with the seams). `--apply` is idempotent, so it's a no-op when the
seams are already present, and it re-applies all three (including the wrap-style streaming seam).

**For a plain git checkout.** A `post-merge` hook re-applies right after the update pulls:

```sh
# hermes-agent/.git/hooks/post-merge   (chmod +x)
#!/bin/sh
ROOT="$(git rev-parse --show-toplevel)"
python /path/to/HermesCloak/install/apply_hooks.py --apply  --hermes-root "$ROOT"
python /path/to/HermesCloak/install/apply_hooks.py --verify --hermes-root "$ROOT"
```

A periodic `--verify` (cron) that alerts on a non-zero exit is a cheap safety net between updates.

## If you can't re-apply right now

`echo off > $HERMES_HOME/cloak/MODE` is harmless when the seams are gone (there's nothing to turn
off), but the meaningful state is: **seams present + MODE=enforce = protected; seams missing =
unprotected**. If an update lands and you can't re-apply immediately, assume unprotected and avoid
sending sensitive matters through that agent until `--verify` is green again.

## Why updates clobber it (and why there's no cleaner hook)

hermes-agent ships no plugin/extension point at these code paths, and its codex transport is
non-standard streaming (so an external proxy can't wrap it). In-source seams are currently the
only integration; the cost is this re-apply-after-update step. If a future hermes-agent exposes a
stable hook API, prefer it over source insertion.
