# Surviving hermes-agent version updates

Architecture v2 modifies **no hermes source**, so the v1 failure mode — an update rewriting the
seam files — is gone. What remains after any update is exactly one hard risk and two soft ones:

1. **The update rebuilds the agent's venv**, wiping the installed `hermescloak` package. The engine
   shim and the `.pth` autoloader both degrade to silent no-ops when the package cannot import —
   masking is completely off while everything *looks* installed. This has happened in the field
   (the 0.19.0 venv rebuild ran unmasked for a week behind green checks). A rebuild also deletes
   the `.pth` from the venv's site-packages.
2. The update could delete untracked files under `plugins/context_engine/` (has not happened; a
   plain `git pull` leaves untracked files alone).
3. Upstream could change the `ContextEngine.select_context()` contract. It is a documented, tested
   ABC — changes should be visible in release notes, and the engine subclasses the built-in
   compressor, so a contract break surfaces as the engine failing to load, not as silent
   passthrough... with one exception: `context.engine` falling back to `compressor` on a load
   failure IS silent passthrough. The doctor checks for that.

## Procedure for every hermes-agent update

```bash
# 1. update hermes-agent as usual

# 2. run the doctor — it proves each link of the runtime chain and exits non-zero on failure:
#    package importable BY THE AGENT'S VENV · engine shim present ·
#    context.engine: cloak selected · .pth autoloader actually patching httpx
python install/apply_hooks.py --verify --hermes-root $HERMES_HOME/hermes-agent

# 3. if the package is BROKEN (venv was rebuilt):
$HERMES_HOME/hermes-agent/venv/bin/python -m pip install -e /path/to/HermesCloak

# 4. if the shim or .pth is MISSING:
python install/apply_hooks.py --apply --hermes-root $HERMES_HOME/hermes-agent

# 5. re-run --verify until it prints "all four links live ✓", then restart the gateway

# 6. confirm on a real turn — the only check that cannot lie:
tail $HERMES_HOME/cloak/audit.log
#    fresh enforce_send lines with "real_values_in_outbound": 0
```

**A stale `audit.log` mtime while gateways are serving is proof the cloak is off**, whatever any
installer output says. Alert on it (a cron `--verify` with non-zero-exit alerting is a cheap net,
and cheaper still: alert when audit.log hasn't grown in N hours of gateway uptime).

## v1 seam remnants

A checkout that still carries the old seams (e.g. via a rebased local commit) is harmless: seams
B/C import functions that no longer exist and no-op through their own `except`, and seam A's
re-masking of already-masked text is idempotent. `--verify` lists the remnants; drop them from the
carried patch whenever convenient.
