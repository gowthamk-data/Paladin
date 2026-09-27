# PALADIN verification report — 2026-09-27 (part 1 of 2)

> **SUPERSEDED by the Phase 6 follow-up pass.** The authoritative record is
> [`2026-09-28-phase6-followup.md`](./2026-09-28-phase6-followup.md), which
> re-verified the suites against a populated Chroma collection and corrected
> the test-count claim below. The orphan-row fix documented here remains valid.
> This document is retained for history only. Where the two disagree, the
> follow-up report wins.
>
> Original supersession note: the authoritative Phase 6 record is
> [`2026-09-27-phase6-closing.md`](./2026-09-27-phase6-closing.md).

Date (UTC): 2026-09-27. Scope: offline PII unit suite, authoritative live
auth suite, authoritative live rate-limit/token suite, and live integration
suite in non-disruptive fast mode. Evidence only; assumptions are labeled.

## Revision

- Branch `main` at `1221347` (one commit ahead of sanitized root `69d8f2d`).
- Working tree at test time: clean. Pre-existing user changes: none pending.

## Environment

- Host: Windows + Docker Desktop (x86_64). OCI ARM64 (2 OCPU / 12 GB) was
  NOT tested here; ARM64 validation remains explicitly outstanding.
- Stack (local): all five compose services `running/healthy`.
- Models present: `llama3:latest` (4.7 GB) only. Active config:
  `OLLAMA_MODEL=llama3.2:3b` (local `.env` default), a tag that is NOT
  pulled. This single mismatch explains every LLM-dependent failure below.
  No model was pulled during verification (left for the operator).
- Hygiene: no secrets, tokens, keys, or passwords recorded here. DB checks
  used counts and foreign-key joins only.

## Commands (all from `paladin/`) and results

### 1. Offline PII unit suite — PASS

`python -m unittest tests.test_pii_detection -v` → `Ran 17 tests ... OK`
(17/17: Luhn, Verhoeff/Aadhaar, card/email/phone/SSN, masks, prompt-guard

### 3. Authoritative live rate-limit/token suite — FAIL (environment, not code)

`python tests/test_rate_limit_and_tokens_live.py` → FAIL in
`part3_token_logging`. Observed: `request_logs` row persisted with
`status=success` but `prompt_tokens=0`, `completion_tokens=0` (`[FAIL]` x2),
then the cross-check raised `ServiceUnavailableError: LLM backend returned
HTTP 404` because the gateway requested `llama3.2:3b`, which is not pulled
(only `llama3:latest` is). Cause: model mismatch, not a code regression; the
gateway behaved as designed (typed 503 surface, row still audited). Green
path: align `.env` with the pulled tag and rerun (not done here to avoid
mutating local `.env`). Proposal only, no code changed: optional startup
check in `OllamaClient` verifying the configured model exists in
`ollama list` before any `/ask` writes a zero-token row.

### 4. Live integration, non-disruptive fast mode — 32/33 (1 known artifact)

`python tests/test_integration_live.py --fast --no-disruption` → 32 passed,
1 failed: `every PII redaction event belongs to a request that was logged
(orphans=1)`. The orphan is `pii_events.id=6` (CREDIT_CARD, 2026-09-25): PII
rows commit BEFORE the LLM call by design, so any earlier run whose LLM call
failed after redaction leaves a PII row with no `request_logs` parent, and
the check has no run window. Everything else passed: error-body hygiene
(401/403/413/422), size guards, metrics aggregation (requests=204,
denials=84, pii_events=11), secret sweep (no password/key material),
port isolation (no host ports for postgres/ollama; gateway on :8000).
Proposal only: (a) scope the orphan check to the current run window, or
(b) write a `RequestLog(status=llm_error)` on downstream failure. No code
changed.

## Suites not run (with reasons)

- Full disruptive integration: SKIPPED (stops/restarts chromadb + ollama;
  reloads 4.7 GB model on CPU). Non-disruptive mode run instead.
- `test_rate_limit_burst_live.py`, `test_ask_e2e_live.py`: SKIPPED
  (superseded smokes; authoritative coverage is items 3–4).
- pytest collection of live harnesses: N/A (`conftest.py` ignores by design).

## Known limitations

- x86_64 Docker Desktop evidence only; OCI ARM64 checks outstanding.
- Live suites append audit rows by design; counts are point-in-time.
- Rate-limit budget is shared: space suites minutes apart or restart
  `paladin-api` to avoid 429 cross-talk.
- `docker compose config` clean (exit 0); `git remote -v` empty; nothing
  committed, pushed, or deployed during verification.

allow/block, context labeling). Note: PowerShell reports exit code 1 from
unittest's stderr stream; the suite itself reports OK.

### 2. Authoritative live auth suite — PASS (15/15)

`python tests/test_auth_live.py` → `Total: 15 | Passed: 15 | Failed: 0`.
Covers emp1/mgr1/admin1 login, 9-claim JWT validation, `/auth/me`, and all
rejection paths. Writes expected `auth_failure_logs` rows.

(Part 2 continues with items 3–4, skipped suites, and limitations.)
