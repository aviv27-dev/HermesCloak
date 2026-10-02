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
  profile.yaml    # copy of profiles/example.yaml, adapted   (never_mask, fail_mode, token_instruction)
  gazetteer.txt   # optional: known names, one "surface<TAB>type" per line (e.g. client list)
  ner_url         # optional: URL of the Hebrew NER microservice (see hermescloak/service/ner_service.py)
```

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

## 4. Verify it's live and healthy

The adapter writes an audit log (event kinds, counts, entity types — **never real PII**):

```bash
tail $HERMES_HOME/cloak/audit.log
# plugin_active → which interception points are live on this hermes version
# enforce_send  → "real_values_in_outbound": 0   (no detected value reached the cloud)
# enforce_restore → "leftover": 0                (every token restored; >0 is the fail-safe signal)
```

`real_values_in_outbound` counts *detected* values that survived into the cloud-bound copy — it
should always be `0`. A non-empty `leftover` means a token reached the reply unrestored (the
fail-safe alarm — investigate).

For an end-to-end proof on your hermes version (a real agent turn against a local fake
"cloud" that records what it receives; uses a throwaway HERMES_HOME):

```bash
/path/to/hermes-venv/bin/python install/e2e_check.py --hermes-root /path/to/hermes-agent
```

## Not covered (know these)

- **Codex app-server runtime** (`codex_app_server`): the Codex subprocess talks to OpenAI itself;
  hermes never sees the request, so it cannot be tokenized. Don't use that runtime for sensitive matters.
- **Mixture-of-Agents prepared requests** and **streaming auxiliary calls** bypass the funnels above.
- Codex `provider_data` replay items (e.g. `codex_message_items`) keep the model's tokenized text;
  they are replayed to the model as-is (consistent, since the vault is durable) but are stored with tokens.
- One vault per agent and process: separate processes (gateway + cron + CLI) on one HERMES_HOME
  each keep their own in-memory map and the last writer's file wins — run sensitive work in one process.

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
