# HermesCloak roadmap

Direction for improving HermesCloak after the v0.1.0 release. Ordered by priority. The guiding
principle is unchanged: **a leaked identity is the catastrophic failure** — so most items push
recall (catch more PII) and close egress paths, without sacrificing the fail-open safety model.

## Near-term

### 1. English / Latin-script NER  ⭐ (headline)
Today, names are caught by the gazetteer or the **Hebrew** NER (DictaBERT); an English name not in
the gazetteer (e.g. `John Doe`) **passes through**. Add an English name recognizer so Latin-script
PERSON/ORG entities are tokenized like Hebrew ones.
- **Approach:** add a recognizer alongside `recognizers/ner.py` behind the `[ner]` extra. Candidates:
  - **GLiNER** (zero-shot, multilingual, permissive) — one model for many entity types/languages.
  - a compact English NER (`dslim/bert-base-NER`, Apache-2.0) or spaCy `en_core_web_trf`.
- **Design:** language-detect per message (or run both); merge spans with the existing
  Hebrew/deterministic recognizers via the current span-resolution (longest/earliest wins).
- **Tests:** extend the QA corpus + golden-leak suite with English/mixed cases; the `english_name`
  cases (currently documented "known gap, passes through") should flip to **masked**.

### 2. Configurable token-label language / scheme
Type labels are Hebrew-only (`⟦לקוח_1⟧`, `⟦מייל_1⟧`). Make the label set configurable per profile
(`he` / `en` / custom), so international users get `⟦CLIENT_1⟧`, `⟦EMAIL_1⟧`. Keep value-keyed
coreference stable across the scheme.

### 3. Transliteration & mixed-script names
Catch a Hebrew client written in Latin ("Dana Cohen" ↔ "דנה כהן") and vice-versa — generate gazetteer variants
(transliteration) and/or fuzzy-match, so a name known in one script is masked in both.

### 4. Single-given-name recall
The one open QA miss (a lone first name like "תמר", also a common word). Add context-gated handling
(role cues like "הלקוח <name>", title prefixes) without raising false positives on common words.

## Mid-term

### 5. Tool-egress guard (the architectural gap)
The seams protect the **LLM prompt** path, not what a *tool* does with a restored value (web search,
third-party email/API send a real name out, by design). Add an optional egress policy: classify
tools as internal/external and require approval (or re-tokenize) for identifying values leaving via
an external tool. This closes the one gap the prompt-note in AGENT-PROMPT.md can only mitigate.

### 6. Gazetteer auto-refresh from source
Productionize `adapter/sql_source.py` so the name list stays current as new clients/parties are
added (scheduled refresh + cache), since the gazetteer is the recall floor. Document the
file/callable/DB `EntitySource` options.

### 7. CI + packaging
GitHub Actions running the 89 tests + the golden-leak suite + the 100-case QA on every push;
pre-commit (ruff/format); publish to **PyPI** (`pip install hermescloak`) with pinned extras.

## Longer-term

### 8. Broader entity coverage / Presidio interop
Optional bridge to Microsoft Presidio recognizers for additional entity types and languages, kept
behind an extra so the dependency-light core is unchanged.

### 9. Observability & alerting
Structured audit metrics (counts, leftover-rate, fail-open events) + ready-made alert sinks beyond
the file/callback alerter; a small dashboard for the `enforce_send`/`leftover` signals.

### 10. Compliance envelope
A DPIA template and a mapping of HermesCloak's controls to the relevant regimes (e.g. GDPR, Israel's
PPA AI guidance), making explicit what the filter does and does **not** satisfy — so adopters in
regulated settings know the policy/vendor-no-train/disclosure pieces they still owe.

---
Contributions welcome. The non-negotiables for any change: keep the core dependency-light, keep
every seam **fail-open**, and never regress the golden-leak suite.
