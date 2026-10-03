# Surviving hermes-agent version updates

## Since HermesCloak 0.2 — plugin install (current hermes-agent)

HermesCloak is a hermes **plugin** now ([INTEGRATION.md](INTEGRATION.md)): it modifies no hermes
file, so a hermes update no longer strips it. What an update *can* still do is move one of the
internal points the plugin hooks. That never breaks hermes (every hook is guarded), but coverage
would drop — so after each update:

```bash
python install/apply_hooks.py --verify --hermes-root /path/to/hermes-agent   # static check, exit 1 = act
/path/to/hermes-venv/bin/python install/e2e_check.py --hermes-root /path/to/hermes-agent   # live proof
grep -E 'plugin_active|seam_missing' $HERMES_HOME/cloak/audit.log | tail -2  # after the gateway restart
```

Also re-run `pip install -e /path/to/HermesCloak` if the update recreated hermes' virtualenv
(`--verify` reports `hermescloak not importable` in that case).

The existing automation keeps working unchanged — `apply_hooks.py --apply` now just (re-)enables
the plugin and is idempotent:

```ini
# /etc/systemd/system/<gateway>.service.d/42-cloak-reapply.conf
[Service]
ExecStartPre=-/path/to/hermes-venv/bin/python /path/to/HermesCloak/install/apply_hooks.py --apply
```

### Migrating from the 0.1 source seams

The old seams anchored on code that no longer exists in hermes-agent (`build_api_kwargs`
docstring, `assistant_message = normalized`, `agent.stream_delta_callback = _stream_delta_cb`), so
on current hermes they cannot be applied and **the privacy layer has been off since that update**.
Steps: install the package into hermes' venv → `hermes plugins enable hermescloak` → restart the
gateway → check `plugin_active` in the audit log. Leftover seams from an old checkout are harmless
(restore is idempotent) and disappear on the next hermes update.

## If you can't verify right now

`echo off > $HERMES_HOME/cloak/MODE` is the kill switch. The meaningful state is: **plugin enabled
+ `--verify` green + MODE=enforce = protected**; anything else = assume unprotected and avoid
sending sensitive matters through that agent until it is green again.
