# Integrating HermesCloak into hermes-agent

HermesCloak is a plain Python library: it tokenizes PII in the messages an agent sends to a
cloud model, and restores the real values in the response — so the cloud provider never sees
client identity, while the agent and its tools keep working with real data.

hermes-agent has no plugin system, so HermesCloak is wired in via **three small, fail-open
call-sites ("seams")** in the agent source. Each seam is wrapped in `try/except: pass`, so it
can never raise into the agent, and it is **MODE-scoped** — an agent whose `$HERMES_HOME/cloak/`
dir is absent or whose `MODE` is `off` is completely unaffected.

> A hermes-agent version update overwrites the files these seams live in. See
> **[UPGRADING.md](UPGRADING.md)** — re-run the installer after every update.

## 1. Install the package (editable)

```bash
pip install -e /path/to/HermesCloak        # exposes `hermescloak` to the agent's venv
```

## 2. Configure the deployment (per agent, NOT in this repo)

Everything that identifies a deployment lives under `$HERMES_HOME/cloak/` — never in the repo:

```
$HERMES_HOME/cloak/
  MODE            # one of: off | shadow | enforce   (read live, per turn — no restart to change)
  profile.yaml    # copy of profiles/example.yaml, adapted   (never_mask, languages, fail_mode, alerts)
  gazetteer.txt   # optional: known names, one "surface<TAB>type" per line (e.g. client list)
  ner_url         # optional: URL of the Hebrew NER microservice (see hermescloak/service/ner_service.py)
```

- **`off`** (or dir absent) → passthrough, zero behaviour change.
- **`shadow`** → run detection and audit *counts/types only* (never real PII), but send the
  original to the cloud and restore nothing. Use this first to prove detection on real traffic
  at zero risk.
- **`enforce`** → tokenize outbound, restore inbound. Flip `shadow`→`enforce` with
  `echo enforce > $HERMES_HOME/cloak/MODE` — no restart (MODE is read every turn).
- **Kill switch:** `echo off > $HERMES_HOME/cloak/MODE` (instant).

## 3. Install the seams

```bash
python install/apply_hooks.py --apply --hermes-root /path/to/hermes-agent
python install/apply_hooks.py --verify        # authoritative present/missing check
```

The three seams (`install/apply_hooks.py --print` emits the exact blocks):

| Seam | File | What it does |
|------|------|--------------|
| **A — outbound** | `agent/chat_completion_helpers.py` (`build_api_kwargs`) | tokenize a **copy** of the messages before the transport encodes them — one choke point covering chat-completions / anthropic / codex / bedrock. |
| **B — inbound** | `agent/conversation_loop.py` (after the assistant message is normalized) | restore real values in the reply **content + tool-call arguments**, before anything is persisted, executed, or sent. |
| **C — streaming** | `gateway/run.py` (wraps `stream_delta_callback`) | restore `⟦tokens⟧` inside streamed deltas so the user-facing reply shows real values (buffers a token split across deltas). |

After installing, **restart the gateway** so the agent process loads the patched files.

## 4. Verify it's live and healthy

The adapter writes an audit log (event kinds, counts, entity types — **never real PII**):

```bash
tail $HERMES_HOME/cloak/audit.log
# enforce_send  → "real_values_in_outbound": 0   (no detected value reached the cloud)
# enforce_restore → "leftover": 0                (every token restored; >0 is the fail-safe signal)
```

`real_values_in_outbound` counts *detected* values that survived into the cloud-bound copy — it
should always be `0`. A non-empty `leftover` means a token reached the reply unrestored (the
fail-safe alarm — investigate).

## Why a library, not a proxy

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
