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
CI already runs the full suite (247 tests, incl. the golden-leak suite and the fictional verdict
corpus) on Python 3.10–3.13; add 3.14 (hermes' managed runtime) and a job that installs the plugin
with `hermes plugins install` against current hermes-agent. Pre-commit (ruff/format); PyPI with
pinned extras (see Visibility & adoption, step 3).

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

## Visibility & adoption

The code is ahead of its discoverability: nothing on the repository page tells a visitor what it
does in ten seconds, and a hermes user searching for a privacy plugin will not find it.

### Repository topics (GitHub → About → ⚙ Topics)
`pii` · `privacy` · `pseudonymization` · `anonymization` · `data-protection` · `redaction` ·
`llm` · `llm-security` · `ai-agents` · `hermes-agent` · `hermes-plugin` · `hebrew` · `nlp` ·
`legal-tech` · `gdpr`

### Suggested steps, by impact

1. **A short GIF or video of the real thing.** Split screen: on the left, a Telegram chat with the
   agent ("what is the client's phone in case 2?") and the answer with the real name and number;
   on the right, the recording proxy's view of the same turn — only `⟦לקוח_1⟧` / `⟦טלפון_1⟧` left
   the machine — then `apply_hooks.py --verify` going green. That contrast is the product in one
   image. Needs a capture on the real deployment (fictional data only); a 20-second shot list can
   be written on request.
2. **English-first surface.** README opens with an English one-liner and quickstart; token labels
   configurable to English (`⟦CLIENT_1⟧`, item 2 above) and English names caught (item 1). Hebrew
   stays the differentiator, not the barrier.
3. **Release v0.2.0 with one-command install.** A GitHub Release with a changelog; install is
   already one command (`hermes plugins install aviv27-dev/HermesCloak --enable`). Then submit to
   **the Hermes plugin catalog** (`plugin-catalog/` in hermes-agent, see its
   `catalog-submission.md`) so `hermes plugins search pii` finds it — hermes' equivalent of Homebrew.
   PyPI (`pip install hermescloak`) after that, for non-hermes users of the library.
4. **A ready adapter for other agents.** The engine is agent-agnostic; a small adapter for one more
   popular agent stack (e.g. an OpenAI-compatible proxy mode, or a LangChain / LiteLLM callback)
   widens the audience beyond hermes users.
5. **Social preview image** (Settings → General → Social preview): the before/after token picture,
   so a shared link explains itself.
6. **Post in the communities where the users are.**
   * Show HN on Hacker News ("reversible PII pseudonymization for LLM agents, Hebrew-first").
   * Reddit: r/LocalLLaMA, r/privacy, r/LegalTech, r/selfhosted.
   * Nous Research Discord, `#plugins-skills-and-skins` (the channel hermes' plugin guide points to).
   * Israeli legal-tech and privacy circles — the first adopters are law and accounting offices.
7. **Contribute back to hermes-agent.** This work found a real upstream gap: when a plugin fails to
   import, hermes logs a warning and carries on, so a privacy plugin can vanish silently (HermesCloak
   now ships its own fail-closed stand-in). Proposing a `plugins.required` / fail-closed option
   upstream as a PR both fixes it for every security plugin and introduces the project to hermes'
   maintainers.
8. **Awesome lists:** awesome-llm-security, awesome-privacy, awesome-ai-agents, and any
   awesome-hermes list.
9. **Trust signals on the repository:** the CI status badge in the README (tests already run on
   Python 3.10–3.13 — add 3.14, which hermes' runtime uses); `CONTRIBUTING.md`; issue templates
   (bug report / false negative — "a value that leaked" — with a "use fictional data only"
   reminder); `SECURITY.md` with a private disclosure address, which a privacy tool especially
   needs; `CHANGELOG.md`.

---
Contributions welcome. The non-negotiables for any change: keep the core dependency-light, keep
every seam **fail-open**, and never regress the golden-leak suite.
