# HermesCloak

One-way PII masking for [Hermes](https://github.com/) LLM agents — the agent reasons in the cloud
over **stable placeholders**; real personal data never leaves your machine in a prompt, and is
substituted back **only at true egress** (the actual outbound action: the email being sent, the API
being called). Replies, transcripts and files keep the placeholders — like a redacted legal filing.

## Example

HermesCloak replaces detected PII with stable, opaque tokens of the form `⟦TYPE_n⟧` (same value →
same token, so the model can still reason about who's who):

```text
in :  "Email jane@firm.org, call 050-1234567, card 4111 1111 1111 1111"
out:  "Email ⟦EMAIL_1⟧, call ⟦PHONE_1⟧, card ⟦CARD_1⟧"     ← only this reaches the cloud model
       ↑ the reply keeps the tokens; the real value re-enters ONLY inside an outbound action
         (e.g. the ⟦EMAIL_1⟧ in a sendMail body becomes jane@firm.org as the mail leaves)
```

Structured identifiers (email, phone, credit card, national ID, case numbers) are detected
language-independently; personal names are added via a gazetteer + NER. The same with a name:

```text
in :  "Client Dana Cohen, ID 000000018, phone 050-1234567"
out:  "Client ⟦CLIENT_1⟧, ID ⟦ID_1⟧, phone ⟦PHONE_1⟧"
```

> **Note on token labels.** Examples here use English labels for readability. The **built-in labels
> are currently Hebrew**: `⟦לקוח⟧`=client, `⟦תז⟧`=national-ID, `⟦חפ⟧`=company-number, `⟦מייל⟧`=email,
> `⟦טלפון⟧`=phone, `⟦אשראי⟧`=card, `⟦תיק⟧`=case. Configurable label language and English name-detection
> (NER) are on the [roadmap](docs/ROADMAP.md) — today, **personal names are detected in Hebrew**, while
> the structured identifiers above work in any language.

## How it works — one-way masking, egress-only restore

There is **no restore-on-return**. The model works in token-space and its replies keep the tokens;
what you gain for that trade is that the whole fragile rehydration machinery (response rewriting,
streaming-delta buffering, per-reply restore passes) simply does not exist:

1. **Outbound (mask):** a *copy* of the messages headed to the model has detected PII replaced with
   stable tokens like `⟦CLIENT_1⟧`, `⟦ID_1⟧` (same value → same token — coreference for the model,
   and a stable prompt-cache prefix). The canonical conversation is never mutated. Handles both
   plain-string and multimodal (list-of-blocks) message content.
2. **Return (nothing):** the reply — text and tool-call arguments — is kept exactly as the model
   produced it, tokens included. Transcripts, session titles, drafted files read like a redacted
   filing: `"נקבע דיון ל⟦לקוח_1⟧"`.
3. **Egress (restore):** when the agent performs an outbound **action** — sends the mail, posts to
   an API — any token in that action's body is substituted with the real value at the moment it
   leaves (via the HTTP-layer egress hooks and `hermescloak.egress`). A token that cannot be
   restored is audited as `leftover`, never dropped silently.

The token→real map (the per-agent vault, `0600` on disk) exists solely for step 3.

```python
from hermescloak import Engine, Profile, StaticFileSource

eng = Engine(profile=Profile.from_yaml("profiles/example.yaml"),
             entity_source=StaticFileSource("names.txt"))

outbound = eng.sanitize_outbound(messages)   # send `outbound` to the cloud model; that's it
# no inbound step — restore happens only at egress (hermescloak.egress / the HTTP hooks)
```

## Detection

- **Deterministic (language-independent):** Israeli national ID (*Teudat Zehut*, with check-digit),
  phone, email, credit card (Luhn), case/docket numbers, land-registry parcel (*Gush*/*Helka*).
- **Gazetteer:** order-independent (surname-first vs given-first) + proclitic-aware (handles glued
  one-letter Hebrew prefixes, e.g. *ל/ב/ו* attached to a name). Fed by a pluggable `EntitySource`
  (file / callable / your own DB adapter).
- **NER (optional `[ner]` extra):** Hebrew personal-name detection via DictaBERT-NER (lazy-loaded;
  runs as a separate shared service, not in-process). English NER is on the roadmap, not yet wired.
  Not required for the core.
- **Never-mask allowlist** (e.g. court/authority names) and an over-mask bias for *names* (a leaked
  identity is the catastrophic failure). Numeric detectors are precise to avoid shredding data dumps.
- **Neutral typing for ambiguous IDs:** a bare 9-digit number (an Israeli national ID and a company
  number are indistinguishable by shape) is tokenized as a neutral `⟦ID⟧`, never a guessed type; the
  model reads the real type from surrounding cleartext. A specific `⟦COMPANY-ID⟧`/`⟦NATIONAL-ID⟧` is
  used only when a label sits next to the number.

The core has **no heavy dependencies** — it is plain Python + `pyyaml`. It does **not** use Presidio
or spaCy; recognizers are built in. The optional Hebrew NER pulls `transformers`/`torch`.

## Use it with hermes-agent — no source seams

Earlier versions patched three "seams" into hermes-agent source files; every hermes update rewrote
them (0.20.0 broke one anchor outright), so the privacy layer had to be re-applied — or carried as a
rebased local commit — after every update. **v2 modifies no hermes source at all.** Everything that
identifies a deployment (profile, name gazetteer, MODE) lives under `$HERMES_HOME/cloak/` — never in
this repo.

**1. Context-engine plugin (main loop).** hermes ships a documented per-turn hook,
`ContextEngine.select_context()`, that may replace the request message list for a single provider
call while the persisted transcript stays untouched — exactly the outbound-masking contract.
`CloakContextEngine` subclasses the built-in compressor (compression behaviour inherited unchanged)
and masks through that hook. It is registered by a ten-line, logic-free shim in
`<hermes>/plugins/context_engine/cloak/` — untracked, survives `git pull` — and selected with
`context.engine: cloak` in config.yaml. A documented ABC survives refactors; an anchor line does not.

**2. Transport hook (everything else).** A patch on `httpx` — the library under the OpenAI SDK, the
Anthropic SDK and the native Gemini adapter — covers what the agent loop never sees: hermes reaches
models from ~120 auxiliary call sites (title generation, context compression, vision, web-extract,
approval, MCP…), and `trajectory_compressor` calls the client directly. Chat-shaped bodies are
**masked** (message subtrees only — tool schemas and params stay byte-identical); non-chat bodies
carrying ⟦tokens⟧ are **egress-restored** so actions leave with real values. Call sites added by
future hermes versions are covered the moment they send a request. The companion `requests` patch
does the same egress restore for model-written scripts.

```bash
python install/apply_hooks.py --apply  --hermes-root /path/to/hermes-agent   # shim + .pth
python install/apply_hooks.py --verify --hermes-root /path/to/hermes-agent   # the doctor
# then: context.engine: cloak in config.yaml, and restart the gateway
```

Known gaps: providers that do not use httpx or requests (Bedrock via boto3), non-Python
subprocesses, and tokens written into local files (deliberate — artifacts stay redacted; use
`python -m hermescloak.egress restore <file>` when a real-values copy is needed).

- **[docs/INTEGRATION.md](docs/INTEGRATION.md)** — install the package, configure
  `$HERMES_HOME/cloak/`, run the installer, verify via the audit log.
- **[docs/AGENT-PROMPT.md](docs/AGENT-PROMPT.md)** — the automatic cloud-model token instruction +
  an optional system-prompt note so the agent doesn't defeat or exfiltrate around the filter.
- **[docs/UPGRADING.md](docs/UPGRADING.md)** — what a hermes-agent update can still silently break
  (venv rebuilds wiping the package) and the one command that proves the cloak is live.

## Try it — browser demo

A stdlib-only demo drives the sanitize → restore lifecycle from a web page, and lets you step
through ~100 QA cases with a live PASS/FAIL per case:

```bash
python -m demo.test_server      # then open http://127.0.0.1:8770
```

## Honest limits — read this

HermesCloak is **risk reduction, not a guarantee and not a compliance certification.**

- It protects the **primary AI model**. If the agent calls an *outbound* tool (e.g. a web search by
  a client's name), the restored real value reaches *that* service by design — "protected from the
  model" ≠ "protected from every third party the agent calls."
- Name detection on messy text is **good but leaky**: a brand-new name not in your gazetteer and
  missed by NER **can leak**. Structured IDs/phones/case-numbers are caught by pattern regardless.
  The gazetteer + deterministic recognizers are the real safety floor — keep your name list current.
- For regulated use (e.g. a law firm: privilege/Bar confidentiality, GDPR, PPA) a filter is one
  piece — you still need the policy/DPIA/vendor-no-train/disclosure envelope around it.

## License & attribution

**Apache-2.0.** Dependencies are all permissive and compatible:
- core runtime: `pyyaml` (MIT) only.
- optional `[ner]`: `transformers` (Apache-2.0), `torch` (BSD), and the **dicta-il/dictabert-ner**
  model by DICTA ([model](https://huggingface.co/dicta-il/dictabert-ner) ·
  [project](https://dicta.org.il/dicta-bert)) — **CC BY 4.0, requires attribution + citation**
  (Shmidman, Shmidman & Koppel, 2023; full text + BibTeX in [`NOTICE`](NOTICE)).
- no copyleft (GPL/LGPL) dependencies, and no third-party code is vendored.
