# HermesCloak

Reversible PII pseudonymization for [Hermes](https://github.com/) LLM agents — so an agent can
reason in the cloud over **placeholders** while real personal data never leaves your machine in the
prompt, and is rehydrated the instant the model replies.

## Example

HermesCloak replaces detected PII with stable, opaque tokens of the form `⟦TYPE_n⟧` (same value →
same token, so the model can still reason about who's who):

```text
in :  "Email jane@firm.org, call 050-1234567, card 4111 1111 1111 1111"
out:  "Email ⟦EMAIL_1⟧, call ⟦PHONE_1⟧, card ⟦CARD_1⟧"     ← only this reaches the cloud model
       ↑ the model's reply is rehydrated back to the real values before you see, persist, or act on it
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

## How it works — contained placeholder lifecycle

Placeholders exist **only on the wire to and from the cloud model**. Your conversation, memory, and
files always hold the **real** values. HermesCloak:

1. **Outbound:** builds a *copy* of the messages headed to the model and replaces detected PII with
   stable tokens like `⟦CLIENT_1⟧`, `⟦ID_1⟧` (same value → same token, for coreference). The canonical
   conversation is never mutated.
2. **Inbound:** the instant the response returns, it rehydrates **everything** — reply text *and
   tool-call arguments* — before anything is persisted, executed, or sent. So when the agent writes
   a file, sends an email, or runs a tool, that action uses the **real** value.

A token therefore never touches disk. (This is the deliberate fix for the failure mode where
pseudonymization tokens leak into persisted files/memory.)

```python
from hermescloak import Engine, Profile, StaticFileSource

eng = Engine(profile=Profile.from_yaml("profiles/example.yaml"),
             entity_source=StaticFileSource("names.txt"))

outbound = eng.sanitize_outbound(messages)        # send `outbound` to the cloud model
restored, report = eng.restore_inbound(response)  # rehydrate before persist/execute/send
if report.leftover:                               # fail-safe signal (see "Honest limits")
    ...  # alert / log
```

## Detection

- **Deterministic (language-independent):** Israeli national ID (*Teudat Zehut*, with check-digit),
  phone, email, credit card (Luhn), case/docket numbers, land-registry parcel (*Gush*/*Helka*),
  and credentials (OpenAI/Anthropic/AWS/GitHub/Slack/Google/Stripe/Telegram keys, JWTs, private
  keys, `password=…` values).
- **Gazetteer:** order-independent (surname-first vs given-first) + proclitic-aware (handles glued
  one-letter Hebrew prefixes, e.g. *ל/ב/ו* attached to a name). Fed by a pluggable `EntitySource`
  (file / callable / your own DB adapter).
- **NER (optional `[ner]` extra):** Hebrew personal-name detection via DictaBERT-NER (lazy-loaded;
  runs as a separate shared service, not in-process). English NER is on the roadmap, not yet wired.
  Not required for the core.
- **Israeli phones in any spelling:** `+972 (0)54-776-5611`, `972-54-7765611`, `054.776.5611`, `00972-3-…`
  are normalized to one national number and validated (mobile/VoIP 9 digits, landline 8) before masking;
  dates, amounts and case numbers never match. A labelled ת"ז written with its leading zeros dropped
  (`ת.ז. 0000018`) is padded and check-digit-tested.
- **Institutional mail stays readable:** addresses at `gov.il`, `muni.il`, `knesset.il`, `idf.il`
  (configurable, suffix match) are not personal data — a court's automated sender keeps its name.
- **Optional second opinion on the masked request** (`jev_check`): TypeSafe's Jev, a typed-decision
  model, is asked whether a private person's name / contact / identifier is still in clear in the text
  *as it leaves* (tokens, not values) — the gaps regexes and a client list cannot close. Off by default;
  the same door serves the office's own locally hosted decision model (`decide_backend: local`), with
  shadow mode and per-use calibration to earn the switch.
- **Never-mask allowlist** (e.g. court/authority names) and an over-mask bias for *names* (a leaked
  identity is the catastrophic failure). Numeric detectors are precise to avoid shredding data dumps.
- **Neutral typing for ambiguous IDs:** a bare 9-digit number (an Israeli national ID and a company
  number are indistinguishable by shape) is tokenized as a neutral `⟦ID⟧`, never a guessed type; the
  model reads the real type from surrounding cleartext. A specific `⟦COMPANY-ID⟧`/`⟦NATIONAL-ID⟧` is
  used only when a label sits next to the number.

The core has **no heavy dependencies** — it is plain Python + `pyyaml`. It does **not** use Presidio
or spaCy; recognizers are built in. The optional Hebrew NER pulls `transformers`/`torch`.

## Use it with hermes-agent

HermesCloak loads as a **hermes-agent plugin** — it modifies no hermes file, so hermes updates no
longer silently remove it. Everything that identifies a deployment (profile, name gazetteer, MODE)
lives under `$HERMES_HOME/cloak/` — never in this repo.

```bash
/path/to/hermes-venv/bin/pip install -e /path/to/HermesCloak
hermes plugins enable hermescloak            # or: python install/apply_hooks.py --apply
python install/apply_hooks.py --verify --hermes-root /path/to/hermes-agent
```

- **[docs/INTEGRATION.md](docs/INTEGRATION.md)** — install, configure `$HERMES_HOME/cloak/`, what is
  intercepted (outbound middleware, inbound, streaming, auxiliary calls), verification, known gaps.
- **[docs/AGENT-PROMPT.md](docs/AGENT-PROMPT.md)** — the automatic cloud-model token instruction +
  an optional system-prompt note so the agent doesn't defeat or exfiltrate around the filter.
- **[docs/UPGRADING.md](docs/UPGRADING.md)** — what to check after a hermes update, and migrating from
  the old source-patch install (which current hermes-agent no longer supports).
- **[docs/LIVE-TESTING.md](docs/LIVE-TESTING.md)** — an acceptance protocol (synthetic data) for a
  running agent: shadow → enforce → tools → replay → secrets → health.

Built to survive failures: a crash-safe, cross-process token vault (write-ahead journal, locked
minting, backups, optional encryption at rest), self-healing config, a NER circuit breaker,
byte-identical replay of the model's own turns, tolerant restore of mangled tokens, an output
audit for PII the model introduced, and official-API backstops — see
[INTEGRATION.md § Reliability](docs/INTEGRATION.md#4-reliability--what-survives-what).

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
