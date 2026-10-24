# Install and test HermesCloak on a stock hermes-agent

One install path, one test path. Everything below is plain `hermes` commands. Nothing is
`pip install`ed by hand and no hermes file is edited.

## Install

```bash
hermes plugins install aviv27-dev/HermesCloak --enable      # per agent: hermes -p <profile> plugins install …
echo enforce > ~/.hermes/cloak/MODE                         # profile: ~/.hermes/profiles/<profile>/cloak/MODE
hermes gateway restart                                      # per agent: hermes -p <profile> gateway restart
```

- **What the install does.** `hermes plugins install` clones this repository to
  `$HERMES_HOME/plugins/hermescloak` and runs hermes' install-time security scan (the tree scans
  **safe**). The plugin is enabled for that home only.
- **What it does not do.** `plugin.yaml` declares `python_runtime: external`, so hermes' package
  manager installs **nothing**. The runtime environment hermes shares between all agents is not
  rebuilt, and the repository's `pyproject.toml` is ignored. The `hermescloak` package is loaded from
  the plugin directory itself. Its only third-party need, YAML, comes from hermes' own `hermes_yaml`.
- **Choosing the agents.** Each agent (profile) has its own `plugins/` directory and its own
  `cloak/MODE`. Install into, and set `enforce` for, only the agents that should be filtered.
- **Configuration.** `profile.yaml`, the client list (`gazetteer.txt`), NER and encryption are
  optional; see [INTEGRATION.md](INTEGRATION.md) §2. Without them you get the built-in detectors and
  default settings.

Update: `hermes plugins update hermescloak`, then `hermes gateway restart`.

### Check that the shared runtime is untouched (before and after)

```bash
ls -d ~/.hermes/installs/*/environments/*/                                   # run before AND after
ls ~/.hermes/installs/*/environments/*/venv/lib/python*/site-packages | grep -i hermescloak   # expect nothing
```

The list of environment directories must be identical before and after the install, with no new
generation and the same environment ids. The second command must print nothing.
`hermes hermescloak verify` prints the same check as `[OK ] shared runtime … contains no hermescloak
package`. `install/stock_install_check.sh` runs the whole cycle on a stock hermes in a throwaway
`$HOME`: install, the before/after comparison, verify, test, remove. CI runs it on every push to
`main` and weekly (`.github/workflows/install-check.yml`).

## What it needs

Nothing to install by hand. The plugin uses the Python standard library plus:

| Need | Comes from | If it is missing |
|---|---|---|
| YAML (config files) | PyYAML when present, else hermes' own `hermes_yaml` | never missing inside hermes |
| `cryptography`, only for vault encryption at rest | hermes' stock runtime ships it | the vault refuses to write (`vault_crypto_unavailable` in the audit log) and keeps tokens in memory only; nothing is written in clear |
| Hebrew NER, optional | a separate service you run (below); `transformers` + `torch` live there, not in hermes | names not in the client list are not caught; structured identifiers still are |

If the plugin cannot load at all (a broken checkout, a missing file), hermes would normally carry on
without it. HermesCloak registers a stand-in instead. In `MODE=enforce` the stand-in **withholds
every request** (main turn and auxiliary calls) and writes `plugin_load_failed` and `blocked_send`
to the audit log. The model receives only the notice *"[HermesCloak: request withheld — the privacy
plugin failed to load …]"*, never your text, so the reply you get is about that notice.
`hermes hermescloak verify` reports the incident. In `off` / `shadow` it changes nothing.

### Optional: Hebrew NER service

Not needed. The built-in detectors catch IDs, phones, e-mail, cards, case numbers, addresses, bank
accounts, plates and dates of birth, and the client list (`cloak/gazetteer.txt`) catches known names.
NER adds names that are on no list. It runs as **one shared local service for all agents**, in its
own Python environment, never inside hermes:

```bash
python3 -m venv ~/cloak-ner && ~/cloak-ner/bin/pip install "transformers>=5.10" "torch>=2.13" pyyaml
cd ~/.hermes/plugins/hermescloak && ~/cloak-ner/bin/python -m hermescloak.service.ner_service --port 8011
echo http://127.0.0.1:8011 > ~/.hermes/cloak/ner_url        # per agent; applies live
```

The service binds to 127.0.0.1 only. On first start it creates a random token in
`~/.config/hermescloak/ner.token` (0600; `HERMESCLOAK_NER_TOKEN_FILE` to move it). Agents running as
the same user find it automatically. The service must prove it knows the token before an agent sends
it any text, so a process that grabs the port while the service is down gets nothing.

When the service is down or slow, a circuit breaker skips NER for 30 s after one timeout
(`ner_down` / `ner_up` in the audit log). Messages keep flowing, protected by everything except
NER. `POST /unload` frees the model's RAM. Deleting `ner_url` turns NER off.

## Test

```bash
hermes hermescloak verify     # is it installed, enabled, loadable and hooked into THIS hermes?
hermes hermescloak test       # end-to-end proof against a local fake cloud
```

`verify` exits 1 if the agent is not protected. It checks:
- the plugin is enabled;
- hermes' own runtime can load it;
- every interception point exists in this hermes version;
- `plugins.isolation` / `openai_runtime` don't route around it.

`test` runs real agent turns (streaming, with a tool loop) against a fake model on `127.0.0.1`. It
checks that no real value reaches the "cloud", that the tool received the real values, and that
the replies were restored. Exit 0 = `PROTECTED ✓`.

- **Isolation.** Its home is a throwaway profile under `~/.hermes/profiles/`, always deleted
  afterwards (`HERMESCLOAK_E2E_KEEP=1` keeps it for debugging). Your agents, sessions and keys are
  not touched.
- **Time and disk.** About 10 s (measured 8 s; `verify` about 2 s). Peak scratch disk is about 1 MB:
  the scratch profile borrows hermes' existing runtime instead of building one.
- **Data.** Synthetic only (`ישראל ישראלי`, ID `000000018`, `050-0000000`, a fake key).
- **No window, no network.** It opens no GUI window, and nothing leaves the machine.

The developer suite (`pytest`, 252 tests) is the same: synthetic or fictional data
(`tests/fixtures/verdicts` is generated), no window, no network beyond `127.0.0.1`. It runs in
about 30 s.

**On a live agent** (synthetic data only), send one message with the test identity from
[LIVE-TESTING.md](LIVE-TESTING.md). Then check:

```bash
tail -3 ~/.hermes/cloak/audit.log      # expect plugin_active, then enforce_send with "real_values_in_outbound": 0
```

## Remove completely

For each agent that has it (`hermes -p <profile> …` for a profile):

```bash
hermes plugins remove hermescloak     # deletes plugins/hermescloak and drops it from plugins.enabled
hermes gateway restart
```

Then delete that agent's data folder `cloak/`: `~/.hermes/cloak`, or
`~/.hermes/profiles/<profile>/cloak`. It holds MODE, profile.yaml, gazetteer.txt, ner_url, the token
vault (`vaults/`), `replay.json` and `audit.log`. If you configured a vault key *file* outside it
(`cloak/vault_key_file`), delete that file too. Stop the NER service if you ran one.

Hooks: the plugin installs none. Remove any you added yourself for 0.1 / early 0.2, such as a systemd
drop-in calling `install/apply_hooks.py --apply` (e.g.
`/etc/systemd/system/<gateway>.service.d/42-cloak-reapply.conf`) or a git `post-merge` hook in the
hermes checkout.

**Check that nothing is left** (every line should print nothing):

```bash
find ~/.hermes -iname '*hermescloak*' -o -type d -name cloak -path '*/.hermes*'
grep -n hermescloak ~/.hermes/config.yaml ~/.hermes/profiles/*/config.yaml
ls ~/.hermes/installs/*/environments/*/venv/lib/python*/site-packages | grep -i hermescloak
grep -rl "apply_hooks.py\|HermesCloak" /etc/systemd/system 2>/dev/null; ls "${TMPDIR:-/tmp}" | grep hermescloak
```

Earlier installs left files behind in more places. Check for these, and remove what you find:

| Left by | Where | Remove with |
|---|---|---|
| `pip install -e …/HermesCloak` (0.1 / early 0.2) | hermes' venv | `<that venv>/bin/pip uninstall hermescloak` |
| a 0.2.0 directory install (no `python_runtime: external`) | hermes' **shared** runtime: `ls ~/.hermes/installs/*/environments/*/venv/lib/python*/site-packages \| grep -i hermescloak` | `hermes plugins remove hermescloak` in every agent that had it (removal alone leaves the copy in the runtime), then `hermes pm install`, which publishes a fresh runtime generation without it (it may also fetch hermes' default optional tools), then `hermes gateway restart` |
| the 0.1 source seams | lines marked `HermesCloak:` in the hermes checkout | `git -C <hermes checkout> checkout -- .`, or the next `hermes update` |
| `install/e2e_check.py` before 0.2.1 | `$TMPDIR/hermescloak-e2e-*` (each up to ~1.8 GB) | `rm -rf "${TMPDIR:-/tmp}"/hermescloak-e2e-*` |
