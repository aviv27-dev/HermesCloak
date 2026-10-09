# Integrating HermesCloak into hermes-agent

HermesCloak is a plain Python library: it tokenizes PII in the messages an agent sends to a
cloud model, and restores the real values in the response — so the cloud provider never sees
client identity, while the agent and its tools keep working with real data.

HermesCloak loads as a **hermes-agent plugin** (current hermes-agent has a plugin system with
request middleware). Nothing in the hermes-agent checkout is modified, so **hermes updates no
longer remove it**. Every interception point is **fail-open** (it can never raise into the agent;
`fail_mode: closed` in the profile withholds text instead of sending it unfiltered) and
**MODE-scoped**: an agent whose `$HERMES_HOME/cloak/` dir is absent or whose `MODE` is `off`
is completely unaffected.

> Upgrading from the old source-patch install? See **[UPGRADING.md](UPGRADING.md)** — the old
> `gateway/run.py` / `conversation_loop.py` anchors no longer exist in hermes-agent, so the
> seams cannot be (and need not be) re-applied.

## 1. Install the package into hermes' Python environment

```bash
/path/to/hermes-venv/bin/pip install -e /path/to/HermesCloak
```

This registers the plugin through the `hermes_agent.plugins` entry point. Plugins are opt-in,
so enable it (step 3).

## 2. Configure the deployment (per agent, NOT in this repo)

Everything that identifies a deployment lives under `$HERMES_HOME/cloak/` — never in the repo
(with hermes *profiles*, that is the profile's home; HermesCloak resolves it the same way hermes does):

```
$HERMES_HOME/cloak/
  MODE            # one of: off | shadow | enforce   (read live, per turn — no restart to change)
  profile.yaml    # copy of profiles/example.yaml, adapted   (never_mask, never_mask_domains, fail_mode, jev_check…)
  gazetteer.txt   # optional: known names, one "surface<TAB>type" per line (e.g. client list)
  ner_url         # optional: URL of the Hebrew NER microservice (see hermescloak/service/ner_service.py)
  vault_key_file  # optional: PATH of a Fernet key file → vault encrypted at rest (see below)
  vault_mode      # optional: "memory" = never persist the token map (rollback switch)
  # written by HermesCloak:
  vaults/         # the per-agent token map: <id>.json snapshot + .journal + .bak  (0600, TTL from last use)
  replay.json     # replay cache: hash(restored text) → the model's own tokenized text
  audit.log       # JSONL events, counts/types only — never real values (rotates at 5 MB × 3)
```

Edits to `profile.yaml`, `gazetteer.txt` and `ner_url` apply **live** to running sessions. A broken
edit never switches filtering off: the last good version keeps being used and `config_error` is
audited.

- **`off`** (or dir absent) → passthrough, zero behaviour change.
- **`shadow`** → run detection and audit *counts/types only* (never real PII), but send the
  original to the cloud and restore nothing. Use this first to prove detection on real traffic.
- **`enforce`** → tokenize outbound, restore inbound. Flip with
  `echo enforce > $HERMES_HOME/cloak/MODE` — no restart (MODE is read every turn).
- **Kill switch:** `echo off > $HERMES_HOME/cloak/MODE` (instant).

## 3. Enable the plugin

```bash
hermes plugins enable hermescloak
# or, equivalently (also what systemd/post-merge automation should call):
python install/apply_hooks.py --apply
python install/apply_hooks.py --verify --hermes-root /path/to/hermes-agent
```

Restart the gateway so the agent process loads the plugin.

What the plugin intercepts:

| Point | hermes-agent mechanism | What it does |
|-------|------------------------|--------------|
| **Outbound** | `llm_request` middleware (official plugin API) | tokenize the final provider request for every transport (chat-completions, Anthropic, Responses/codex, Bedrock): string **and multi-part** content, prior **tool-call arguments**, `tool_use`/`tool_result` blocks, Responses `input` items, system/instructions. Signed reasoning, images and tool schemas are never touched. |
| **Inbound** | wraps `normalize_response` of every registered transport (incl. ones registered later by provider plugins) | restore real values in reply **content, reasoning and tool-call arguments** (also `\u27e6`-escaped tokens) before anything is persisted, executed, or sent. |
| **Streaming** | wraps `AIAgent._fire_stream_delta` / `_emit_stream_end` | restore tokens in streamed deltas for **every** consumer (gateway, TUI, API server, CLI, TTS); holds a token split across chunks and **flushes the tail at stream end**. |
| **Auxiliary** | wraps the auxiliary client's completion funnel | context **compression**, session **titles**, vision, approval… previously sent the raw conversation to a cloud model; now tokenized out and restored back. |

The plugin writes `plugin_active` to the audit log on load, listing each point; if a hermes
update ever removes one, it is reported as **`seam_missing`** (and logged as a warning) instead of
silently reducing coverage.

## 4. Reliability — what survives what

| Failure | Behaviour |
|---------|-----------|
| gateway restart / crash / `kill -9` mid-write | every new mapping is fsync'd to the vault journal **before** its token is used; a torn last line is skipped; snapshot written twice (main + `.bak`) atomically. Verified by a chaos test (4 processes, random SIGKILL, ~28k tokens: 0 lost, 0 reused). |
| several processes on one HERMES_HOME (gateway + cron + CLI + subagents) | minting and every disk read happen under an exclusive file lock; a token minted elsewhere is picked up on demand — never the same token for two values |
| vault file corrupted | quarantined as `.corrupt-<ts>`, `.bak` loaded instead (`vault_corrupt_quarantined`, `vault_restored_from_backup`) |
| encrypted vault, key missing/wrong (e.g. a cron job without the key env) | that process goes memory-only, **never overwrites** the encrypted data, and mints from a far-away number range so its tokens cannot collide with the readable vault's (`vault_locked`); a corrupt encrypted snapshot falls back to `.bak` |
| vault idle / swept / deleted while in use | the TTL counts from last **use** (restores and reuse refresh it); sweeps re-check under the vault lock; a live process that finds its file gone rewrites it (`vault_resurrected`) |
| broken `profile.yaml` / unreadable `gazetteer.txt` | last good version kept (`config_error`) |
| NER service hung or down | circuit breaker: one timeout, then NER is skipped for 30 s (`ner_down` / `ner_up`) instead of a timeout per message |
| internal filter error | `fail_mode: open` → original sent + `unfiltered_sent`; `fail_mode: closed` → text withheld + `blocked_send` (also with `jev_action: block` on a Jev hit) |
| hermes update moves an interception point | `seam_missing` at load + warning; two official-API backstops still restore tool arguments (`tool_request` middleware) and the final reply (`transform_llm_output`) |
| concurrent sessions in one process | the vault is thread-safe (one token per value, ever) |
| long-running gateway | engines, content caches and the replay cache are bounded LRUs; audit log rotates |

### Encryption at rest

```bash
python -c "from hermescloak.durable_vault import generate_key; print(generate_key())" > /secure/place/cloak.key
chmod 600 /secure/place/cloak.key
echo /secure/place/cloak.key > $HERMES_HOME/cloak/vault_key_file      # or env HERMESCLOAK_VAULT_KEY_FILE
pip install 'hermescloak[crypto]'                                      # the cryptography package
```

Keep the key **outside** the cloak dir (it protects a copied/backed-up disk, not a compromised
running process). Egress scripts read the same key setting.

## 5. Verify it's live and healthy

The adapter writes an audit log (event kinds, counts, entity types — **never real PII**):

```bash
tail $HERMES_HOME/cloak/audit.log
# plugin_active → which interception points are live on this hermes version (+ self_test)
# enforce_send  → "real_values_in_outbound": 0   (no detected value reached the cloud)
# enforce_restore → "leftover": 0                (every token restored; >0 is the fail-safe signal)
```

`real_values_in_outbound` counts *detected* values that survived into the cloud-bound copy — it
should always be `0`. A non-empty `leftover` means a token reached the reply unrestored (the
fail-safe alarm — investigate).

| Event | Meaning |
|-------|---------|
| `plugin_active` | plugin loaded: per-interception-point status + `self_test` |
| `enforce_send` | a request was tokenized: `in_request` (distinct tokens per type in THIS request), `in_system_prompt` (the part of those inside the system prompt, e.g. names in SOUL.md), `entities` (cumulative distinct values in the vault, all turns so far — not this turn), `real_values_in_outbound` (must be 0), `replayed` |
| `jev_residual` | (`jev_check: true`) the typed-decision second opinion on the masked request: `probs` per question (person / contact / identifier still in clear), `hits` ≥ `jev_min_confidence`, `ms`, `model` |
| `jev_unavailable` | the second opinion did not run: `no-key`, `timeout`, `breaker`, `busy`, `error` — the turn went on unchecked (rate-limited) |
| `enforce_restore` | a reply was restored; `leftover` > 0 = alarm |
| `leftover_token` | which token(s) could not be restored |
| `leaked_original` | the model wrote a value we had masked — it saw it via some unmasked path: **investigate** |
| `new_pii` | PII-shaped values the model introduced itself (counts per type) |
| `backstop_restore` | a backstop caught a token the primary restore missed (should be rare) |
| `unfiltered_sent` / `blocked_send` | an internal error; sent unfiltered (fail-open) / withheld (fail-closed) |
| `seam_missing`, `config_error`, `ner_down`, `vault_*` | see the reliability table above |

`python install/apply_hooks.py --verify --strict` summarizes all of this (vault health, last plugin
load, incidents in the last 24 h) and exits non-zero on any incident — wire it to cron/monitoring.

For an end-to-end proof on your hermes version (a real agent turn against a local fake
"cloud" that records what it receives; uses a throwaway HERMES_HOME):

```bash
/path/to/hermes-venv/bin/python install/e2e_check.py --hermes-root /path/to/hermes-agent
```

## Not covered (know these)

- **Codex app-server runtime** (`model.openai_runtime: codex_app_server`, `/codex-runtime on`): the
  Codex subprocess talks to OpenAI itself; hermes never sees the request, so it cannot be tokenized.
  `apply_hooks.py --verify` prints a WARN for such a profile (`--strict` fails). The default Codex
  OAuth route (`codex_responses`, runtime `auto`) IS covered. Don't use the app-server runtime for
  sensitive matters.
- **Mixture-of-Agents prepared requests** and **streaming auxiliary calls** bypass the funnels above.
- Codex `provider_data` replay items (e.g. `codex_message_items`) keep the model's tokenized text;
  they are replayed to the model as-is (consistent, since the vault is durable) but are stored with tokens.

## Why in-process, not a proxy

hermes-agent's codex transport is non-standard streaming; no OpenAI-compatible proxy can wrap it
transparently. The in-process seams see the canonical message list before transport, so the same
integration covers every backend.

## Honest limits

Detection is strong on structured identifiers (national ID with check-digit, phone, email, credit
card via Luhn, case numbers) and on names that are in the gazetteer or caught by the NER model.
It is **not airtight**: names not known to the gazetteer/NER, transliterated/foreign-script name
forms, and free-text quasi-identifiers can pass through. Treat HermesCloak as strong risk
**reduction**, not a guarantee — and see [AGENT-PROMPT.md](AGENT-PROMPT.md) for keeping the agent
from defeating it.

### Optional second opinion: a typed-decision model on the masked request (`jev_check`)

The two gaps the deterministic layer cannot close — a first name on its own, and a party whose name
is not on the client list (NER off) — are exactly what a reader of the *tokenized* text can still
notice. With `jev_check: true` and `OPENROUTER_API_KEY` in the agent's environment, every request is
also shown, **as it leaves (tokens, not values)**, to TypeSafe's Jev (a System One model: typed,
calibrated yes/no answers, ~0.3–0.6 s, via OpenRouter `POST /api/alpha/decisions`), asked three
questions: is a private person's name / a personal contact detail / a personal identifier still in
clear. Answers ≥ `jev_min_confidence` are hits; `jev_action: audit` logs them (`jev_residual`),
`jev_action: block` withholds the request like `fail_mode: closed`. Guards (`hermescloak/decide.py`):
`jev_timeout_s` (default 3 s) then the turn goes on unchecked, a circuit breaker (3 failures → 10 min
off), at most 2 calls in flight, no key → nothing sent, never raises. Off by default: a second vendor
sees the masked text. Measure it on your own data before trusting it (`install/jev_corpus_check.py`).

Institutional mail domains (`never_mask_domains`, default `gov.il`, `muni.il`, `knesset.il`, `idf.il`,
suffix match) are left in clear: a court's automated sender or a ministry is not personal data, and
masking it costs a mail summary its sender. Set `never_mask_domains: []` to mask every address.
