# Integrating HermesCloak into hermes-agent

HermesCloak is a plain Python library: it tokenizes PII in everything an agent sends to a cloud
model, and substitutes the real values back **only at egress** — inside the outbound action (the
mail being sent, the API being posted to). The cloud provider never sees client identity; replies
and transcripts stay in redacted token-space; actions still work with real data.

**No hermes source is modified.** Two attachment points, both fail-open and **MODE-scoped** — an
agent whose `$HERMES_HOME/cloak/` dir is absent or whose `MODE` is `off` is completely unaffected:

| # | Mechanism | Covers |
|---|-----------|--------|
| 1 | **Context-engine plugin** — `CloakContextEngine` masks via hermes's documented `select_context()` hook (replaces the request list per call; persisted transcript untouched). Registered by a logic-free shim in `<hermes>/plugins/context_engine/cloak/`; selected by `context.engine: cloak`. Subclasses the built-in compressor, so compression behaviour is unchanged. | main conversation loop, every provider |
| 2 | **HTTP-layer hooks** — an `httpx` patch (loaded at interpreter startup via a `.pth`) masks chat-shaped request bodies (the ~120 auxiliary call sites: titles, compression, vision, web-extract…) and egress-restores ⟦tokens⟧ in non-chat action bodies. A companion `requests` patch egress-restores in model-written scripts. | everything the agent loop never sees |

> What can still break after a hermes update (venv rebuilds!) and the one command that proves the
> cloak is live: **[UPGRADING.md](UPGRADING.md)**.

## 1. Install the package (editable, into the AGENT'S venv)

```bash
$HERMES_HOME/hermes-agent/venv/bin/python -m pip install -e /path/to/HermesCloak
```

## 2. Configure the deployment (per agent, NOT in this repo)

Everything that identifies a deployment lives under `$HERMES_HOME/cloak/` — never in the repo:

```
$HERMES_HOME/cloak/
  MODE            # one of: off | shadow | enforce   (read live — no restart to change)
  profile.yaml    # copy of profiles/example.yaml, adapted   (never_mask, languages, fail_mode, alerts)
  gazetteer.txt   # optional: known names, one "surface<TAB>type" per line (e.g. client list)
  ner_url         # optional: URL of the Hebrew NER microservice (see hermescloak/service/ner_service.py)
  vaults/         # created automatically: per-agent token→real map (0600) — exists ONLY for egress
```

- **`off`** (or dir absent) → passthrough, zero behaviour change.
- **`shadow`** → run detection and audit *counts/types only* (never real PII), but send the
  original to the cloud. Use this first to prove detection on real traffic at zero risk.
- **`enforce`** → mask outbound; egress-restore actions. Flip with
  `echo enforce > $HERMES_HOME/cloak/MODE` — no restart (MODE is re-read within seconds).
- **Kill switch:** `echo off > $HERMES_HOME/cloak/MODE` (instant). Egress restore stays active
  regardless of MODE — restoring a leaked token is always correct.
- **Back up `vaults/` nowhere you wouldn't put the client list itself** — it maps tokens to real
  values. Exclude it from offsite backups or encrypt them.

## 3. Install the engine shim + transport autoloader

```bash
python install/apply_hooks.py --apply --hermes-root /path/to/hermes-agent
# then select the engine in $HERMES_HOME/config.yaml:
#   context:
#     engine: cloak
# and restart the gateway.
python install/apply_hooks.py --verify --hermes-root /path/to/hermes-agent   # the doctor
```

The doctor proves the actual runtime chain (package importable by the agent's venv, shim present,
engine selected, httpx patched at startup) and exits non-zero when the cloak cannot be live.

## 4. Verify it's live and healthy

The adapter writes an audit log (event kinds, counts, entity types — **never real PII**):

```bash
tail $HERMES_HOME/cloak/audit.log
# enforce_send          → "real_values_in_outbound": 0  (main loop; via select_context)
# transport_masked      → auxiliary LLM call masked at the transport
# egress_http_restored  → an action left with real values substituted
# egress_leftover       → a token could NOT be restored at egress — investigate
```

`real_values_in_outbound` counts *detected* values that survived into the cloud-bound copy — it
should always be `0`. Tokens in replies/transcripts are **normal** in v2 (that's the design);
`egress_leftover` is the alarm that matters.

## Why in-process, not a proxy

hermes-agent's codex transport is non-standard streaming; no OpenAI-compatible proxy can wrap it
transparently. The engine hook sees the canonical message list before transport, so the same
integration covers every backend — and the httpx hook catches what never passes the agent loop.

## Honest limits

Detection is strong on structured identifiers (national ID with check-digit, phone, email, credit
card via Luhn, case numbers) and on names that are in the gazetteer or caught by the NER model.
It is **not airtight**: names not known to the gazetteer/NER, transliterated/foreign-script name
forms, and free-text quasi-identifiers can pass through. Bedrock (boto3) and non-Python
subprocesses bypass the HTTP hooks. Treat HermesCloak as strong risk **reduction**, not a
guarantee — and see [AGENT-PROMPT.md](AGENT-PROMPT.md) for keeping the agent from defeating it.
