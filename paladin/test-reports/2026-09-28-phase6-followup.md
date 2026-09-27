# PALADIN Phase 6 verification follow-up — 2026-09-27

Supersedes `2026-09-27-verification.md` and `2026-09-27-phase6-closing.md`
(both now marked superseded). All work is **uncommitted** in the working tree
(`git remote -v` is empty; nothing pushed or deployed).

Environment: local **x86_64** Docker Desktop. **Deployment verdict: LOCAL
ONLY.** No OCI/ARM64 readiness is claimed.

## 0. Correction to earlier records (test claims)

`tests/test_integration_live.py` is a **script**, not a `unittest` module: it
defines `def main()` and contains no `TestCase` subclasses. Importing it via
`python -m unittest` therefore collects **zero** tests and runs **no**
integration checks. Verified:

```
$ python -m unittest tests.test_integration_live -v
Ran 0 tests in 0.000s
NO TESTS RAN
```

Consequences for previously recorded figures:

| Figure | What it actually measures |
| --- | --- |
| "integration 33/33" | the **script** `python tests/test_integration_live.py --fast --no-disruption`, not unittest |
| "21/21 combined" | sum of focused **unit** suites only (`test_pii_detection` + `test_ask_failure_live` + `test_ask_http_503_live`); `test_integration_live` contributed **0** |

These two result classes must not be conflated. The 33/33 integration result
itself was not re-executed in this pass (out of scope by instruction) and is
carried forward as a prior result, not as fresh evidence.

## 1. RAG data verification

Model availability:

```
$ docker exec paladin-ollama-1 ollama list
NAME             ID              SIZE      MODIFIED
llama3.2:3b      a80c4f17acd5    2.0 GB    3 hours ago
llama3:latest    365c0bd3c000    4.7 GB    3 days ago

$ # gateway.config inside the container
OLLAMA_MODEL = llama3.2:3b      # matches a pulled tag
EMBEDDING_MODEL = all-MiniLM-L6-v2
```

The earlier "model mismatch" failure is resolved: the configured default
`llama3.2:3b` is now pulled and present.

Documented ingestion command (corrected README form) is functional and
idempotent:

```
$ docker compose exec -T -e PYTHONPATH=/app paladin-api python data/ingest.py
[INGEST] engineering_policy.txt: 1 chunks
[INGEST] executive_briefing.md: 1 chunks
[INGEST] finance_guidelines.txt: 1 chunks
[INGEST] hr_handbook.md: 1 chunks
[INGEST] Total: 4 chunks ingested.
```

Chroma contents (`count() = 4`, one chunk per document):

| id | clearance_level | department |
| --- | --- | --- |
| `engineering_policy.txt::chunk-0` | 1 | Engineering |
| `hr_handbook.md::chunk-0` | 2 | HR |
| `finance_guidelines.txt::chunk-0` | 2 | Finance |
| `executive_briefing.md::chunk-0` | 3 | Executive |

**Retrieval evidence (distinguishing real retrieval from empty-collection
fallback).** The empty-collection fallback returns "No relevant documents
found…" with zero sources and zero tokens. The query below returns documents
and scales monotonically with the clearance filter, which only a populated,
correctly filtered collection can do:

| user clearance | documents returned | sources |
| --- | --- | --- |
| 1 | 1 | `engineering_policy.txt` |
| 2 | 3 | `hr_handbook.md`, `finance_guidelines.txt`, `engineering_policy.txt` |
| 3 | 4 | all four |

The Level-3-only `executive_briefing.md` is correctly withheld at clearance 1
and 2. **VERDICT: real, clearance-filtered retrieval.**

## 2. Targeted suite execution

### 2a. Rate-limit / token suite

```
$ python tests/test_rate_limit_and_tokens_live.py
...
  checks: 21 | passed: 21 | failed: 0
```

Run against the populated collection, respecting the documented budget. The
suite enforces its own 60s windows and 65s quiet periods to work around the
per-user burst allowance, and the two suites were run **sequentially**, not
concurrently, so they did not compete for the same limiter bucket.

Retrieval and token cross-check evidence from the run:

```
=== PART 3: Ollama token counts written to request_logs (admin1, 4 documents) ===
  /ask -> HTTP 200 (0.61s), sources=['executive_briefing.md', 'finance_guidelines.txt',
                                     'hr_handbook.md', 'engineering_policy.txt']
  direct OllamaClient().chat() -> {'documents_retrieved': 4, 'prompt_eval_count': 350,
                                   'eval_count': 2, ...}
  [PASS] prompt_tokens populated from Ollama (non-zero)      -- prompt_tokens=350
  [PASS] completion_tokens populated from Ollama (non-zero)  -- completion_tokens=2
  [PASS] logged prompt_tokens == Ollama prompt_eval_count    -- db=350 vs ollama=350
  [PASS] logged completion_tokens matches Ollama eval_count  -- db=2 vs ollama=2
  [PASS] token counts scale with the real prompt (1-doc user < 4-doc user)
                                                             -- emp1=162 < admin1=350
```

Requirements met: documents retrieved (**4** for admin1, **1** for emp1), token
counts non-zero (350/2 and 162/2), and logged counts match the direct Ollama
cross-check exactly on `prompt_tokens`.

### 2b. E2E suite

```
$ python tests/test_ask_e2e_live.py
=== Step 1: Authenticate as emp1 ===        Status: 200
=== Step 2: Submit /ask query ===           Status: 200
    Query: What are the key engineering security requirements?
=== Step 3: Response ===
Response: According to the provided context, the key engineering security
requirements are: 1. Access to engineering documents is granted based on
clearance level. 2. Sensitive technical information must be shared only within
the appropriate clearance boundary ... (Source: Engineering Policy) ...
Sources: ['engineering_policy.txt']
Request ID: 8931a63d-25c4-438a-bcd3-7a550e049fcc
TEST PASSED: Valid LLM-generated response received
```

Real-retrieval proof: non-empty `sources`, and the answer's content is specific
to `engineering_policy.txt` (clearance-boundary language) rather than the
generic empty-collection string. Audit row for that request id, read back from
PostgreSQL:

```
status= success  prompt_tokens= 156  completion_tokens= 89
sanitized_prompt= 'What are the key engineering security requirements?'
```

Non-zero tokens on a `success` row further exclude the fallback path.

## 3. HTTP-level 503 coverage — ACHIEVED (not deferred)

New file `tests/test_ask_http_503_live.py` (`AskHttp503Tests`, 2 tests) drives
`/ask` through the **whole FastAPI application** and asserts a real HTTP 503.
`fastapi.testclient.TestClient` is unusable here because it imports `httpx`,
which is not in the runtime image, so the test issues the request through a
small in-process ASGI driver instead. Nothing was added to the image.

What runs for real: routing, the slowapi limiter, `CorrelationMiddleware`,
`LoggingMiddleware`, the `ServiceUnavailableError` handler in
`gateway/errors.py`, and the HTTP status code. Only auth (`get_current_user`)
and the DB session (`get_db`) are overridden, and the DB is an in-memory
SQLite instance — **no row is written to the real PostgreSQL instance and no
historical audit row is touched.**

The downstream failure is injected by patching `VectorStore` / `OllamaClient`
in `gateway.routes.query_routes`, so no container is stopped or restarted.
Both requests are far below the burst allowance, so the limiter is
deliberately left enabled — the 503 path must work *through* it.

```
$ docker compose exec -T -e PYTHONPATH=/app:/tmp paladin-api \
    python -W ignore -m unittest test_ask_http_503_live -v
test_llm_outage_returns_503_with_audit_row ... ok
test_vector_store_outage_returns_503_with_audit_row ... ok
Ran 2 tests in 0.547s
OK
```

Each test asserts: `status_code == 503`; `body["dependency"]` is
`"vector store"` / `"llm"`; no `Traceback` and no raw card number anywhere in
the response; `body["request_id"]` equals the `X-Request-ID` response header;
and the persisted parent row is `status=llm_error`, the fixed safe
`llm_response`, zero tokens, with the PII event still present and the raw
value absent.

The tracebacks visible in stderr are the handler's intentional server-side
`exc_info=True` logging — they are not test failures.

Two harness subtleties worth recording:

- `ask` is a sync route, so Starlette runs it in an anyio worker thread. A
  default SQLite `:memory:` engine gives each thread its own empty database
  ("no such table: users"), so the engine must use `StaticPool` with
  `check_same_thread=False`.
- The ASGI `receive` callable must deliver a `http.disconnect` after the body,
  otherwise the logging middleware's `listen_for_disconnect` task never
  completes and the call hangs forever.

Combined focused unit run — no regression from the new file:

```
$ docker compose exec -T -e PYTHONPATH=/app:/tmp paladin-api \
    python -W ignore -m unittest test_ask_http_503_live test_ask_failure_live test_pii_detection
Ran 23 tests in 0.637s
OK
```

## 4. Local Chroma persistence verification

The container was recreated with the volume strictly preserved. `docker
compose down -v` was **not** used.

Exact command:

```
$ docker compose up -d --force-recreate --no-deps chromadb
 Container paladin-chromadb-1 Recreated
 Container paladin-chromadb-1 Started
```

Before / after evidence:

| | before | after |
| --- | --- | --- |
| container `Created` | (pre-existing) | `2026-09-27T10:48:14Z` (new) |
| container health | healthy | healthy |
| volume name | `paladin_chromadb_data` | `paladin_chromadb_data` |
| volume mount | `/chroma/chroma` | `/chroma/chroma` |
| volume `CreatedAt` | `2026-09-22T17:01:08Z` | `2026-09-22T17:01:08Z` (unchanged) |
| `collection.count()` | 4 | 4 |
| document ids | 4 ids as tabled in §1 | identical 4 ids |

The volume's `CreatedAt` is unchanged, which is the positive proof that the
named volume was reused rather than recreated. A live `/ask` was re-run after
the restart and still returned `Sources: ['engineering_policy.txt']` with a
grounded answer. **Persistence verified locally.**

Caveat: this is a local Docker Desktop check only. It does not establish
persistence on the OCI target, where the storage driver, filesystem and volume
configuration differ.

## 5. Modified files (all uncommitted)

- `paladin/tests/test_ask_http_503_live.py` — **new**, HTTP-level 503 coverage
- `paladin-plan.md` — verification status rewritten with fresh results + the
  `test_integration_live.py` correction
- `paladin/test-reports/2026-09-28-phase6-followup.md` — **new**, this report
- `paladin/test-reports/2026-09-27-phase6-closing.md` — marked superseded
- `paladin/test-reports/2026-09-27-verification.md` — marked superseded
- (pre-existing from the earlier pass, unchanged here) `README.md`,
  `gateway/Dockerfile`, `gateway/routes/query_routes.py`,
  `tests/test_integration_live.py`, `tests/test_pii_detection.py`,
  `tests/test_ask_failure_live.py`

## 6. Outstanding deployment gates

1. **OCI ARM64 build** on the real target (Ampere A1); current evidence is
   x86_64 only.
2. **ARM64 image manifests** pushed/verified for every service image.
3. **`docker stats` + `ollama ps` re-measurement** on the target to confirm
   `mem_limit` / `cpus` and model residency within the 2 OCPU / 12 GB budget.
4. **Chroma persistence re-check on the target** storage driver.
5. **Disruptive outage checks** (the integration suite's Chroma/Ollama
   stop-start path) — not re-run in this pass.
6. **Full integration suite** (`--fast --no-disruption` and the disruptive
   path) re-run on the target.
7. Rate limiter is `memory://`, so it does not scale across workers/replicas;
   TLS, key rotation and DB backup remain unimplemented.

## 7. Verdict

Phase 6 is **GREEN for the local environment**: RAG data verified non-empty
with real clearance-filtered retrieval, both targeted suites pass against that
populated collection with token cross-checks, the `/ask` 503 path is now
covered at the HTTP level, and Chroma persistence survives container
recreation with the volume preserved.

**Deployment status: LOCAL ONLY. OCI ARM64 build, manifest, resource and
persistence verification are OUTSTANDING.** Nothing is committed, pushed or
deployed; all changes are left in the working tree for review.
