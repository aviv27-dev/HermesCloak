# Changelog

All notable changes to HermesCloak. Versions follow [semantic versioning](https://semver.org/).
A release is created automatically when a version in `plugin.yaml` reaches `main` with a section here.

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
