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

## Use it with hermes-agent

HermesCloak wires into a hermes-agent checkout via three small, fail-open seams (it has no plugin
API). Everything that identifies a deployment (profile, name gazetteer, MODE) lives under
`$HERMES_HOME/cloak/` — never in this repo.

- **[docs/INTEGRATION.md](docs/INTEGRATION.md)** — install the package, configure
  `$HERMES_HOME/cloak/`, install the three seams (`install/apply_hooks.py`), verify via the audit log.
- **[docs/AGENT-PROMPT.md](docs/AGENT-PROMPT.md)** — the automatic cloud-model token instruction +
  an optional system-prompt note so the agent doesn't defeat or exfiltrate around the filter.
- **[docs/UPGRADING.md](docs/UPGRADING.md)** — **a hermes-agent update overwrites the seams**; how to
  re-apply + verify after every update so the privacy layer never goes silently off.

```bash
python install/apply_hooks.py --apply  --hermes-root /path/to/hermes-agent
python install/apply_hooks.py --verify --hermes-root /path/to/hermes-agent
```

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
