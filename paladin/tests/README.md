# PALADIN test suite

All tests live in `paladin/tests/`. There are two families, and only one of them can be
collected by a test runner.

| File | Kind | Needs the stack? | Command |
| --- | --- | --- | --- |
| `test_pii_detection.py` | unit (pure Python) | no | `python -m unittest tests.test_pii_detection -v` (or `python -m pytest tests/test_pii_detection.py -v`) |
| `test_abac_policy.py`, `test_abac_vector_store.py`, `test_ingest_manifest.py` | isolated ABAC/ingestion unit tests (pytest style: `parametrize`, `pytest.raises`, `tmp_path`) | no | `python scripts/run_isolated_abac_tests.py` (or, where pytest is installed: `python -m pytest tests/test_abac_policy.py tests/test_abac_vector_store.py tests/test_ingest_manifest.py -v`) |
| `test_abac_secondary_denial.py`, `test_ask_http_503_live.py` | isolated ASGI unittest suites (the denial test injects a deliberately filter-bypassing result) | no live services | `python scripts/run_isolated_abac_tests.py` |
| `test_integration_live.py` | live integration | yes | `python tests/test_integration_live.py` |
| `test_auth_live.py` | live (auth matrix) | yes | `python tests/test_auth_live.py` |
| `test_rate_limit_and_tokens_live.py` | live (rate limit, concurrency, token logging, correlation ids, login throttling) | yes | `python tests/test_rate_limit_and_tokens_live.py [--part0\|--burst\|--extra]` |
| `test_rate_limit_burst_live.py` | live (earlier, simpler burst probe) | yes | `python tests/test_rate_limit_burst_live.py` |
| `test_ask_e2e_live.py` | live (earliest end-to-end smoke test) | yes | `python tests/test_ask_e2e_live.py` |

Every command is run from the project root (`paladin/`, the directory containing
`docker-compose.yml`). The live harnesses print a `PASS`/`FAIL` line per check and exit non-zero
if any check failed, so they can gate a pipeline.

## Isolated ABAC route tests

`python scripts/run_isolated_abac_tests.py` builds the API Dockerfile with a unique temporary image tag and runs the whole ABAC unit-test suite in a disposable container, split by framework:
the pytest-style files (`test_abac_policy`, `test_abac_vector_store`, `test_ingest_manifest`) under pytest, and the unittest-style files (`test_abac_secondary_denial`,
`test_ask_http_503_live`) under `python -m unittest`. pytest is never baked into the production image: the runner downloads the pure-python pytest wheels on the host
into a temporary wheelhouse, mounts it read-only into the disposable container, and installs pytest from those wheels with `pip --no-index` into a tmpfs; the container runs
with `--network none` and therefore cannot reach any package index or service. The host `tests/` directory is mounted read-only; the container uses an in-memory SQLite
session through the tests' dependency overrides and receives no JWT signing keys. The temporary image tag and wheelhouse are removed afterward, and a
zero-collected-tests result is reported as a failure rather than a pass. This runner does not rebuild or restart the Compose API service, touch the `paladin-paladin-api:latest` image, or require PostgreSQL, ChromaDB, or Ollama to be running.

The ABAC secondary-denial test deliberately injects a result that violates the normal Chroma
filter. It verifies the defense-in-depth denial branch, not a normal-path ABAC denial.

## PostgreSQL-backed tests

Two integration paths require throwaway PostgreSQL services and are intentionally skipped when
their explicit connection settings are absent:

* `test_generation_pointer.py::test_acquire_serializes_concurrent_setup_across_processes` uses
  `PALADIN_PG_CONCURRENCY_URL` for a disposable PostgreSQL database. It tests real cross-process
  advisory-lock behavior; SQLite and fakes are not substitutes.
* `test_ask_legacy_pointer_fallback_pg.py` uses `PALADIN_PG_FALLBACK_URL` and
  `PALADIN_CHROMA_FALLBACK_HOST`. Its PostgreSQL URL must target the throwaway service
  `gate2-fallback-pg` and database `paladin_test`; `PALADIN_CHROMA_FALLBACK_HOST` must be
  `gate2-fallback-chroma`. The route, pointer resolution, and audit persistence use real
  PostgreSQL; Chroma retrieval and Ollama generation are mocked by the test.

The isolated runner also accepts `PALADIN_ISOLATED_TEST_NETWORK` to run these tests. It requires
an existing Docker **internal** network named `paladin-abac-scratch-<id>` containing only the
two disposable services. The service/container names must satisfy the runner's guard:
`gate2-fallback-pg` with container name `paladin-abac-pg-<id>`, and `gate2-fallback-chroma`
with container name `paladin-abac-chroma-<id>`. Do not publish ports or attach live volumes.
Set all three URL/host variables in the shell that invokes the runner; the runner passes them
only to its isolated test containers. For example, using an alphanumeric throwaway password:

```powershell
$env:PALADIN_ISOLATED_TEST_NETWORK = 'paladin-abac-scratch-demo1'
$env:PALADIN_PG_CONCURRENCY_URL = 'postgresql+psycopg2://postgres:throwaway-only@gate2-fallback-pg:5432/paladin_test'
$env:PALADIN_PG_FALLBACK_URL = $env:PALADIN_PG_CONCURRENCY_URL
$env:PALADIN_CHROMA_FALLBACK_HOST = 'gate2-fallback-chroma'
python scripts/run_isolated_abac_tests.py
```

Create the named internal network and both private services before the command, then remove the
scratch services, their volumes, and the network afterward. The concurrency test creates and
drops a uniquely named schema in the supplied throwaway database. Never point these variables
at a live or shared database. Without the scratch environment, these PostgreSQL-only cases
remain skipped and the runner correctly reports incomplete verification.

`gateway/requirements.lock` and `ui/requirements.lock` are complete pinned constraints captured
from the working API and UI images. Docker builds install the top-level requirements against
those constraints so transitive dependencies cannot silently drift. In particular, the UI lock
keeps PyArrow at 25.0.1 alongside NumPy 1.26.4; PyArrow 26 requires NumPy 2 at runtime, while the
working Streamlit environment uses NumPy 1.26.4. The UI requirements file does not need a
separate direct NumPy requirement: the lock pins its resolved runtime version. Its Docker build
pins the build-only `wheel` tool to 0.45.1 because the base image's newer wheel requires
`packaging>=24`, which conflicts with Streamlit 1.31's `packaging<24` requirement.

## Why the live harnesses are not collected by pytest

`conftest.py` lists them in `collect_ignore`. They are script-style suites that talk to a running
gateway over HTTP, make real LLM generations and (in the integration suite) stop and start
containers; the older ones execute their checks at import time. Collecting them under pytest
would either do nothing useful or start a live run during collection. Run them explicitly.

## Authority of each file

Nothing here has been silently weakened or deleted. Where two files overlap, the newer/more
thorough one is authoritative and the older one is retained as a quick smoke test:

* **Rate limiting** - `test_rate_limit_and_tokens_live.py` is authoritative: it checks the burst
  allowance, the request beyond it, the `Retry-After` header, per-user isolation, the window
  reset, the sustained limit, the fast-fail (a rejected request never reaches the LLM) and the
  login-throttling bucket. `test_rate_limit_burst_live.py` is the earlier, simpler burst probe
  (20 requests accepted, 21st rejected, window reset after 65 s) and remains only as a quick
  smoke test.
* **Authentication** - `test_auth_live.py` is authoritative for token handling: required claims,
  missing/malformed/tampered/expired tokens, wrong signing algorithm, wrong audience, and JWT
  signature verification. The integration suite adds the response-body hygiene checks on top.
* **PII and injection** - `test_pii_detection.py` is authoritative for the detector's logic
  (including the Luhn and Verhoeff checks and the redaction masks);
  `test_integration_live.py` is authoritative for what the *live system* does with PII
  (masks in `request_logs`, rows in `pii_events`, and the whole-database sweep for raw values).
* **End to end** - `test_integration_live.py` supersedes `test_ask_e2e_live.py`, which is kept as
  a one-command smoke test of login + `/ask`.

## Notes and caveats

* The live suites consume rate-limit budget. `/auth/login` allows 10 requests per minute per IP
  and `/ask` allows a burst of 20 with a sustained 10/minute average per authenticated user, so
  run them a minute apart (or after `docker compose restart paladin-api`) if a run fails with
  HTTP 429.
* `test_integration_live.py --fast` runs only the checks that need no LLM generation: useful as
  a smoke test on a slow CPU-only host.
* `test_integration_live.py --no-disruption` skips the ChromaDB/Ollama outage checks, which stop
  and restart those containers. The first `/ask` after an Ollama restart re-loads the model into
  memory, which on CPU takes a while.
* Set up the model with `./scripts/setup_ollama.sh` (pulls `$OLLAMA_MODEL` inside the ollama
  container): local default is `llama3.2:3b`, the deployed/demo instance uses `llama3`.
* Read-only checks (the unit tests, and anything that only performs `SELECT`s) never mutate the
  audit tables. The live suites intentionally *add* audit rows, which is what makes the
  consistency checks meaningful.
