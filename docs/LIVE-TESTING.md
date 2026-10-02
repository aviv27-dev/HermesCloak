# Live acceptance test on a running hermes agent

A protocol an operator — or a Claude session that manages the hermes deployment — can run
against the **real** agent after installing or upgrading HermesCloak. It uses **synthetic data
only**: never test with a real client's details. Every step says what to expect in
`$HERMES_HOME/cloak/audit.log` (JSONL; counts and types only, never real values).

Rollback at any point: `echo off > $HERMES_HOME/cloak/MODE` (instant, no restart).

## 0. Offline checks (no traffic)

```bash
python install/apply_hooks.py --verify --hermes-root /path/to/hermes-agent      # all OK
/path/to/hermes-venv/bin/python install/e2e_check.py --hermes-root /path/to/hermes-agent
#   real AIAgent, fake cloud, OpenAI + Anthropic streaming with a tool loop → PROTECTED ✓
```

### Optional: sandbox against a real model (no deployment touched)

`install/live_sandbox.sh` installs the latest hermes-agent + HermesCloak into a throwaway venv and
runs `install/live_model_check.py`: real AIAgent conversations against a **real model** (default
Ollama Cloud, `OLLAMA_API_KEY` from the environment; override with `HC_LIVE_BASE_URL`,
`HC_LIVE_API_KEY`, `HC_LIVE_MODEL`) through a local recording proxy that sees the exact bytes
leaving the machine. Hard checks = the privacy guarantee (no synthetic real value in any outbound
request in enforce mode); soft checks = how well that model keeps tokens intact.

```bash
OLLAMA_API_KEY=... bash install/live_sandbox.sh       # set the key in the environment, not in a chat
```

Synthetic test identity (add the name to `$HERMES_HOME/cloak/gazetteer.txt` for the test, remove
it afterwards):

| field | value |
|---|---|
| name | `ישראל ישראלי` |
| national ID (valid check digit) | `000000018` |
| phone | `050-0000000` |
| email | `test.client@example.org` |
| fake API key | `sk-proj-TESTTESTTESTTESTTESTTEST` |

## 1. Shadow mode — prove detection, change nothing

`echo shadow > $HERMES_HOME/cloak/MODE`, restart the gateway once (loads the plugin), then send
the agent (through its normal channel, e.g. Telegram):

> "Summarize: client ישראל ישראלי, ID 000000018, phone 050-0000000, email test.client@example.org"

Expect: `plugin_active` with every point `ok` and `"self_test": "ok"`; then `shadow_detect` with
counts for לקוח / מזהה (or תז) / טלפון / מייל. The reply is normal (nothing was changed).

## 2. Enforce — the cloud sees tokens, you see real values

`echo enforce > $HERMES_HOME/cloak/MODE` (no restart). Send the same message.

Expect:
- `enforce_send` with `"real_values_in_outbound": 0`;
- `enforce_restore` with `"leftover": 0`;
- the reply on the channel shows the **real** name/phone (also while streaming), never `⟦…⟧`.

## 3. Tool call with real values

> "Write a file test-cloak.txt containing the phone and email of ישראל ישראלי"

Expect: the file contains `050-0000000` and `test.client@example.org` (real values reached the
tool); `enforce_send` still `0`; no `leftover_token`.

## 4. Multi-turn / replay

Continue the same conversation for 2–3 more turns that refer back to the client.

Expect: `enforce_send` lines show `"replayed": N` with N ≥ 1 on later turns (the model's own
earlier turns are sent back byte-identically); no `restore_error`.

## 5. Secrets

> "Store this key for later: sk-proj-TESTTESTTESTTESTTESTTEST"

Expect: entity counts include `סוד`; `real_values_in_outbound: 0`.

## 6. Live config edit (no restart)

Append `דנה בדיקה\tלקוח` to `gazetteer.txt`, then mention "דנה בדיקה" in the **same** session.

Expect: the next `enforce_send` counts one more לקוח.

## 7. Health summary

```bash
python install/apply_hooks.py --verify --strict --hermes-root /path/to/hermes-agent
```

Expect exit 0; `new_pii` lines are informational. Any `[WARN]` (unfiltered_sent, leftover_token,
leaked_original, seam_missing, vault_*, config_error, ner_down) is a finding to investigate.

## Cleanup

Remove the synthetic names from `gazetteer.txt`. Test tokens stay in the vault until its TTL
expires (`vault_ttl_hours`), which is harmless.
