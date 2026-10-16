# Surviving hermes-agent version updates

## hermes' managed runtime (hermes-agent, October 2026)

hermes now runs from its own package-manager environment under `$HERMES_HOME/installs/`, not from
the venv it was installed into. An installation made with `pip install -e /path/to/HermesCloak`
into hermes' venv **stops loading silently** after that update: `plugins.enabled` still lists
`hermescloak`, but the runtime cannot import it, and older `--verify` versions still said OK.
Reinstall with `hermes plugins install aviv27-dev/HermesCloak --enable` and confirm with
`hermes hermescloak verify` / `hermes hermescloak test` and a fresh `plugin_active` + `enforce_send`.
0.2.0's directory install let hermes' package manager put the package into the runtime shared by
every agent. 0.2.1 declares `python_runtime: external` and installs nothing; to clear a 0.2.0
leftover, see [INSTALL.md → Remove completely](INSTALL.md#remove-completely).

## Since HermesCloak 0.2 — plugin install (current hermes-agent)

HermesCloak is a hermes **plugin** now ([INTEGRATION.md](INTEGRATION.md)): it modifies no hermes
file, so a hermes update no longer strips it. What an update *can* still do is move one of the
internal points the plugin hooks. That never breaks hermes (every hook is guarded), but coverage
would drop — so after each update:

```bash
hermes hermescloak verify      # static + install check, exit 1 = act
hermes hermescloak test        # end-to-end proof (throwaway home, deleted afterwards)
grep -E 'plugin_active|seam_missing' $HERMES_HOME/cloak/audit.log | tail -2  # after the gateway restart
```

If `--verify` reports `hermescloak importable by hermes' managed runtime … MISSING`, the running
agent is **not** protected — typically HermesCloak was pip-installed into a venv the managed runtime
does not use. Fix: `hermes plugins install aviv27-dev/HermesCloak --enable` (or
`hermes plugins update hermescloak`), restart the gateway, check `plugin_active` in the audit log.

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
Steps: `hermes plugins install aviv27-dev/HermesCloak --enable` → restart the gateway → check
`plugin_active` in the audit log → `apply_hooks.py --verify`. Leftover seams from an old checkout are harmless
(restore is idempotent) and disappear on the next hermes update.

## If you can't verify right now

`echo off > $HERMES_HOME/cloak/MODE` is the kill switch. The meaningful state is: **plugin enabled
+ `--verify` green + MODE=enforce = protected**; anything else = assume unprotected and avoid
sending sensitive matters through that agent until it is green again.
