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

| Figure | Suites actually included | Notes |
| --- | --- | --- |
| "21/21 combined" | `test_pii_detection` + `test_ask_failure_live` **only** | `test_ask_http_503_live.py` did not exist yet, so it contributed **0**. `test_integration_live` contributed **0** (script, not unittest). |
| "23/23 combined" | `test_pii_detection` + `test_ask_failure_live` + `test_ask_http_503_live` | first run that included the new ASGI-level suite |
| "25/25 combined" (current) | `test_pii_detection` + `test_ask_failure_live` + `test_ask_http_503_live` | after adding the 2 initialization-failure tests (§4) |
| "integration 33/33" | **none** — not a unittest run at all | comes from the separately invoked script `python tests/test_integration_live.py --fast --no-disruption` |

Strict rule applied throughout this document: **unittest counts and
integration-script results are never summed or presented as the same class of
evidence.** The 33/33 integration figure was not re-executed in this pass (out
of scope by instruction) and is carried forward as a prior result only — it is
not a new run.

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

**Audit-row user identification.** The row for request id
`8931a63d-25c4-438a-bcd3-7a550e049fcc` was located in `request_logs` and joined
to `users`. Exactly **one** row matches, with `prompt_tokens=156` and
`completion_tokens=89` as recorded above:

| field | value |
| --- | --- |
| `user_id` | 1 |
| **username** | **`emp1`** |
| **clearance_level** | **1** |
| `status` | `success` |

This is consistent with the run: the e2e script authenticates as `emp1`
(clearance 1), which is why only the single clearance-1 document
(`engineering_policy.txt`) was returned. No prompt text, response body,
credential or unrelated audit row is reproduced here.

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

## 4. Audit-failure fix — dependency initialization inside the guard

### 4.1 The defect

`VectorStore()` and `get_embedding_model()` were called **before** the
`try:` block that guards the downstream section:

```python
vector_store = VectorStore()      # <-- outside the guard
model = get_embedding_model()     # <-- outside the guard
try:
    ... retrieval / generation ...
except ServiceUnavailableError:
    _write_llm_error_log(db, request_id, current_user, redacted_prompt)
    raise
```

Both initializers can fail — and `VectorStore.__init__` deliberately raises a
typed `ServiceUnavailableError` when ChromaDB is unreachable. Because the
exception was raised before the guard, it bypassed `_write_llm_error_log`
entirely. Since the PII rows are committed *earlier* in the request, the result
was precisely the orphan the audit check forbids: committed `pii_events` rows
with no parent `request_logs` row. Only `query()`/`chat()` failures were
covered, so a ChromaDB outage detected at connection time escaped the fix.

### 4.2 Embedding-model initialization

`get_embedding_model()` was the weaker link. Unlike `VectorStore` (which wraps
every failure in `ServiceUnavailableError`) and `OllamaClient` (whose `__init__`
cannot fail), it called `sentence_transformers.SentenceTransformer(...)` with
no error handling. A load failure — missing package, corrupt/unreadable weights,
disk full, OOM — propagated as an **untyped** exception, producing a **500**
with a server-side traceback rather than a **503**, and again leaving the
committed PII rows orphaned.

It now converts any load failure into `ServiceUnavailableError("embedding
model")`, logging the original with `exc_info=True` server-side and chaining it
via `from` for the log trail. The original message is never surfaced to the
client: the raised error's `detail` is the fixed
`"embedding model is temporarily unavailable"`, and the generic 503 handler in
`gateway/errors.py` renders only that fixed text.

### 4.3 The fix

Both initializations moved inside the existing guard, so every downstream
failure — construction, model load, embedding, query, generation — now takes
the same typed path: write the sanitized `llm_error` parent row, then re-raise
so the 503 handler responds.

### 4.4 Regression coverage

Two focused tests added to `tests/test_ask_failure_live.py`:

- `test_vector_store_construction_failure_after_pii` — patches only
  `VectorStore` so the **constructor** raises. Asserts the typed path is taken
  (`isinstance` of `ServiceUnavailableError`, `service == "vector store"`), and
  that exactly one PII event and exactly one linked `llm_error` parent row
  exist, with the stored prompt sanitized (`****`, raw card number absent), the
  safe fixed error text, and zero tokens.
- `test_embedding_model_load_failure_after_pii` — lets the **real**
  `get_embedding_model()` run while faking `sentence_transformers` to raise
  `OSError("No space left on device")`. Asserts the typed conversion
  (`service == "embedding model"`), that the underlying OSError text is *not*
  leaked into `exc.detail`, and the same single-PII-event / single-parent-row /
  sanitized / zero-token invariants.

```
$ docker compose exec -T -e PYTHONPATH=/app:/tmp paladin-api \
    python -W ignore -m unittest test_ask_failure_live -v
test_embedding_model_load_failure_after_pii ... ok
test_llm_failure_after_pii ... ok
test_vector_failure_after_pii ... ok
test_vector_store_construction_failure_after_pii ... ok
Ran 4 tests in 0.677s
OK
```

Combined focused run after the change:

```
$ docker compose exec -T -e PYTHONPATH=/app:/tmp paladin-api \
    python -W ignore -m unittest test_pii_detection test_ask_failure_live test_ask_http_503_live
Ran 25 tests in 0.727s
OK
```

The tracebacks printed during these runs are the handlers' intentional
server-side `exc_info=True` logging, not test failures. No LLM suite was run
for this change, as instructed; the only live check was a single `/ask` smoke
test after restarting the API, which returned HTTP 200 with
`Sources: ['engineering_policy.txt']`, confirming the guard move did not
regress the success path.

## 5. Modified files (all uncommitted)

- `paladin/gateway/routes/query_routes.py` — dependency initialization moved
  inside the guarded downstream block; `get_embedding_model()` now converts load
  failures into a typed `ServiceUnavailableError`
- `paladin/tests/test_ask_failure_live.py` — 2 new initialization-failure
  regression tests (4 total in this file)
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
populated collection with token cross-checks, the `/ask` 503 path is covered at
the HTTP level, the downstream-initialization gap is closed and covered by two
further regression tests (focused run 25/25 OK), and Chroma persistence
survives container recreation with the volume preserved.

No check in this pass failed, and the E2E audit row was verified against
PostgreSQL (username `emp1`, clearance 1).

**Deployment status: LOCAL ONLY. OCI ARM64 build, manifest, resource and
persistence verification are OUTSTANDING.** Nothing is committed, pushed or
deployed; all changes are left in the working tree for review.
