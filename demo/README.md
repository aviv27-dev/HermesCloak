# HermesCloak browser demo / test harness

A tiny **stdlib-only** bridge so you can drive the core sanitize → restore lifecycle from a web
page. The core `Engine` is a plain Python library with no HTTP layer; `test_server.py` wraps it
with three JSON endpoints (same `http.server` style as `hermescloak/service/ner_service.py`) and
serves `index.html`. No new dependencies — just `pyyaml` (already a core dep).

## Run

From the repo root:

```
python -m demo.test_server
```

Then open <http://127.0.0.1:8765>.

Options: `--host` / `--port` (defaults `127.0.0.1:8765`).

## What it does

- **① Outbound** — type text with PII, click **Sanitize**. The page shows the tokenized copy that
  would go to the cloud model (`⟦לקוח_1⟧`, `⟦תז_1⟧`, …). Same value → same token.
- **🎲 הרץ בדיקה אקראית / Run random credibility test** — generates a random PII-dense document
  (valid Israeli IDs, phones, emails, Luhn cards, case numbers, gazetteer names + filler, with some
  values repeated), runs the full sanitize → restore round-trip, and shows a PASS/FAIL verdict for
  three invariants: **NO-LEAK** (no real value survives in the outbound copy), **REVERSIBLE**
  (restore reproduces the original exactly), **NO-LEFTOVER** (no unresolved tokens). It mirrors the
  Python fuzzer below.
- **② Inbound** — a simulated model reply that *uses* those tokens; click **Restore** to rehydrate
  the real values, with a banner reporting any **leftover** tokens (the fail-safe signal). Run
  Sanitize first so the session Vault is populated.
- **איפוס סשן / Reset** — drops the session Engine + Vault to start fresh.

Detection in the demo: deterministic (Israeli ת"ז check-digit, phone, email, credit-card Luhn,
case numbers) + a small built-in name gazetteer (`demo_names()` in `test_server.py`). The optional
Hebrew NER microservice is **not** wired into this demo.

## Endpoints (for scripted testing)

| Method | Path           | Body                                          | Returns |
|--------|----------------|-----------------------------------------------|---------|
| POST   | `/api/sanitize`| `{messages:[{role,content}], session}`        | `{messages, vault}` |
| POST   | `/api/restore` | `{response:{content, tool_calls?}, session}`  | `{response, leftover, restored_any}` |
| POST   | `/api/reset`   | `{session}`                                   | `{ok:true}` |
| GET    | `/health`      | —                                             | `{status, sessions}` |

## Bulk credibility fuzzer

`tests/test_credibility_random.py` is a randomized property suite asserting the same invariants over
hundreds of random documents (incl. very long ones, >4000 chars), plus coreference stability,
idempotency, and fail-safe leftover detection.

```
python tests/test_credibility_random.py 800        # standalone, prints a report; random base seed
python tests/test_credibility_random.py 800 --seed 1337   # reproducible
pytest tests/test_credibility_random.py            # if pytest is installed
```

Every failure prints its RNG seed so any red run is reproducible.

> Demo/test harness only — not a production surface.
