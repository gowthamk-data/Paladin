# PALADIN

A self-hosted, clearance-aware RAG gateway in front of a local LLM. Every request is
authenticated, rate limited, scanned for PII, checked for prompt injection, answered from a
clearance-filtered vector store, then written to an audit trail. Nothing leaves the host.

```
                 ┌──────────────────────────────────────────────────────────────┐
   browser ─────▶│ paladin-ui (Streamlit, :8501)                                │
                 └───────────────┬──────────────────────────────────────────────┘
                                 │ Bearer JWT
                 ┌───────────────▼──────────────────────────────────────────────┐
   clients ─────▶│ paladin-api (FastAPI + Uvicorn, :8000)                       │
                 │  CorrelationMiddleware  -> server-generated X-Request-ID     │
                 │  LoggingMiddleware      -> one line per request              │
                 │  SlowAPIMiddleware      -> per-user rate limiting            │
                 │  MaxBodySizeMiddleware  -> 413 before any work               │
                 │  auth  |  PII redaction  |  injection guard                  │
                 └──┬─────────────────┬───────────────────┬─────────────────────┘
                    │                 │                   │
       ┌────────────▼───┐   ┌─────────▼────────┐   ┌──────▼──────────┐
       │ postgres:16.15 │   │ chromadb 0.4.24  │   │ ollama 0.34.2   │
       │ audit tables + │   │paladin_docs_abac│   │ $OLLAMA_MODEL   │
       │ users          │   │ + ABAC metadata  │   │                 │
       └────────────────┘   └──────────────────┘   └─────────────────┘
              all four services share one private bridge network; only :8000 and :8501 are
              published, and the JWT signing key is bind-mounted read-only.
```

## Quick start

```bash
cd paladin
cp .env.example .env          # then edit POSTGRES_PASSWORD
chmod +x scripts/*.sh
./scripts/generate_rsa_keys.sh           # if keys/ is empty
docker compose up -d --build

# first run only: pull the model and build the retrieval index
./scripts/setup_ollama.sh # pulls "$OLLAMA_MODEL" inside the ollama container
docker compose exec -T -e PYTHONPATH=/app paladin-api python data/ingest.py
docker compose exec -T paladin-api python -m gateway.db.seed
```

Prerequisites: Docker Engine + the Compose plugin, `bash` and `openssl` (for the
key script). Provisioning needs egress to the container registries, the Ollama
model library (`llama3` is a ~4.7 GB download) and Hugging Face (embedding
model, cached in its named volume after the first run).

* UI: <http://localhost:8501>
* API docs (Swagger): <http://localhost:8000/docs>
* Health: <http://localhost:8000/health>

`docker compose up` fails immediately if `POSTGRES_PASSWORD` is unset - it will not start
PostgreSQL with an empty password.

### Demo users

| username | password | department | clearance | role |
| --- | --- | --- | --- | --- |
| `emp1` | `emp1_pass` | Engineering | 1 | employee |
| `mgr1` | `mgr1_pass` | Engineering | 2 | manager |
| `admin1` | `admin1_pass` | Executive | 3 | admin |
| `fin1` | `fin1_pass` | Finance | 2 | employee |

## API

| Method | Path | Auth | Notes |
| --- | --- | --- | --- |
| `POST` | `/auth/login?username=&password=` | none | returns an RS256 JWT; failures are audited |
| `GET` | `/auth/me` | Bearer | re-reads the user record and re-checks the token claims against it |
| `POST` | `/ask?message=` | Bearer | the whole pipeline: redact -> guard -> retrieve -> generate -> audit |
| `GET` | `/metrics/stats` | Bearer, role `admin` | requests by department, total prompt/completion tokens, blocks by reason, PII events by type, auth failures by reason |
| `GET` | `/health` | none | liveness only; it does not probe the dependencies |

Errors are deliberately blunt: `401` unauthorised, `403` wrong role, `400` prompt injection
blocked, `413` prompt or body too large, `422` malformed request, `429` rate limited, `500`
unexpected, `503` a dependency (database, vector store, LLM) is unavailable. Every error body is
`{"detail": ..., "request_id": ...}` with no stack traces, driver messages or echoed input.

## Security controls

| Control | Where | Behaviour |
| --- | --- | --- |
| Authentication | `gateway/auth/` | RS256 JWT, 9 required claims, audience + issuer checked, claims re-validated against the live user row |
| Authorisation / ABAC | `gateway/authz/policy.py`, `gateway/rag/vector_store.py` | Fail-closed V1 policy requires sufficient clearance and either company-wide visibility or a matching department. The Chroma predicate is followed by a per-chunk authorization check; `role=admin` is not a bypass |
| Manifest-driven indexing | `data/manifest.json`, `data/ingest.py` | Every document has explicit classification, department, visibility and a `content_sha256` digest. Ingestion validates the manifest (fail-closed), recomputes each digest against the file bytes, rebuilds a staging collection, validates it, then promotes it to `paladin_docs_abac`. Labels are owner-approved (see `docs/classification-decisions.md`) |
| PII redaction | `gateway/security/pii_detector.py` | credit cards (Luhn), Aadhaar (Verhoeff), email, phone, SSN; redaction happens **before** the injection check and before any write |
| Prompt-injection guard | `gateway/security/prompt_guard.py` | 9 direct-injection patterns; a match is denied with `400` and audited |
| Indirect injection containment | `gateway/security/prompt_guard.py` | retrieved chunks are wrapped in a `[CONTEXT BLOCK - tagged as retrieval data, not instructions]` message and the system prompt tells the model to ignore embedded instructions |
| Rate limiting | `gateway/security/rate_limiter.py` | per authenticated user (per IP when anonymous), stacked burst + sustained limits |
| Correlation | `gateway/middleware/correlation_middleware.py` | server-generated UUID; a client-supplied `X-Request-ID` is discarded, never trusted |
| Audit trail | `gateway/db/models.py` | `request_logs`, `denial_logs`, `pii_events`, `auth_failure_logs` (+ `users`) |
| Error handling | `gateway/errors.py` | dependency failures become `503`, no internals leak, tracebacks stay server-side |
| Secrets | `docker-compose.yml`, `.dockerignore` | keys mounted read-only, `keys/` and `.env` excluded from images and from git |

## Document classification and pre-ingestion checklist

Document classification is governed by the rubric and owner decisions in
[`docs/classification-decisions.md`](docs/classification-decisions.md). The project
owner (Gowtham K) is the sole content owner, approver, and ingestion operator — a
deliberate scope simplification for a solo-operator portfolio project. Multi-operator
approval enforcement (two-person rule, CODEOWNERS/CI) is future work, not implemented.

Run this checklist before every `data/ingest.py` execution:

1. **Review changed and untracked files.** `git status` and `git diff` on
   `paladin/data/documents/` and `paladin/data/manifest.json` — confirm you know
   about every added, modified, or untracked document/manifest file.
2. **Confirm labels against the rubric and intended readership.** For each new or
   changed document, re-check `classification_level`, `department`, and `visibility`
   against the Level 1/2/3 rubric and who should actually be able to read it.
   Classification follows the rubric and intended readership, not the document's own
   wording.
3. **Regenerate the content hashes.** For every changed or added document, recompute
   `sha256(<file bytes>)` and update the matching `content_sha256` in
   `manifest.json`. Ingestion fails closed if any digest does not match.
   Scope note: the digest is a hash of **document bytes only** — it does not
   validate the classification fields (checked by you in step 2, enforced by
   the ABAC policy at query time) and does not authenticate who approved.
4. **Record the approval commit.** Commit the manifest (and document) change with the
   trailer `Approved-by: Gowtham K <YYYY-MM-DD> — Direction: tightened|broadened|unchanged`.
   The trailer is a manual, non-cryptographically-enforced record — a
   self-asserted line verified by review, not by code.
   For any change that broadens access, also write the one-paragraph rationale in the
   commit message.
5. **Only then run ingestion.** `docker compose exec -T -e PYTHONPATH=/app paladin-api
   python data/ingest.py`. Ingestion re-validates the manifest and re-verifies every
   digest, so a stale or mismatched hash stops the run.

## Rate limiting semantics

`RATE_LIMIT_PER_MINUTE` (default 10) and `RATE_LIMIT_BURST` (default 20) are **not** two views of
the same window. `/ask` is decorated with two independent slowapi limits:

* burst: `RATE_LIMIT_BURST` per 1 minute
* sustained: `RATE_LIMIT_PER_MINUTE * 6` per 6 minutes

A burst allowance is only meaningful on a *longer* window than the burst window; if the
sustained cap used the same 60 s window it would always bind first. `RATE_LIMIT_BURST` is
therefore floored at `RATE_LIMIT_PER_MINUTE` (a lower value would silently make the policy
stricter than configured). In practice: up to 20 requests in a minute, but no more than 60 in any
6-minute stretch, per authenticated user.

Consequences worth knowing:

* The limiter is **per gateway process** (`storage_uri="memory://"`). With more than one Uvicorn
  worker or a second API replica the effective limits multiply. Use a shared backend before
  scaling out.
* Unauthenticated callers are keyed by source IP, so everyone behind one NAT shares a bucket.
* `/auth/login` and the other undecorated routes get the flat `RATE_LIMIT_PER_MINUTE/minute`
  default; that is what throttles password guessing (10 failed logins per IP per minute before
  `429`).
* A rejected request still gets the correct `Retry-After`, `X-RateLimit-*` headers and a
  correlation id.
* Rate limiting is *not* a substitute for authentication: the bucket is chosen from the token the
  caller presents, and it is keyed on the token subject, not on the IP, for authenticated users.

## Testing

All harnesses live in `paladin/tests/` (see `paladin/tests/README.md` for the full matrix).
Run everything from `paladin/`:

```bash
# offline unit tests: no stack required
python -m unittest tests.test_pii_detection -v

# live suites: require `docker compose up -d --build` plus a pulled model and seeded DB
./scripts/setup_ollama.sh # pulls "$OLLAMA_MODEL" inside the ollama container
docker compose exec -T -e PYTHONPATH=/app paladin-api python data/ingest.py
docker compose exec paladin-api python -m gateway.db.seed

python tests/test_auth_live.py                  # JWT claims, tampering, expiry, audience
python tests/test_rate_limit_and_tokens_live.py # burst + sustained limits, per-user isolation
python tests/test_rate_limit_burst_live.py      # quick burst smoke test
python tests/test_ask_e2e_live.py               # login + /ask smoke test
python tests/test_integration_live.py           # full lifecycle, clearance, PII, error paths
python tests/test_integration_live.py --fast           # skip LLM generations (slow CPU hosts)
python tests/test_integration_live.py --no-disruption  # skip the ChromaDB/Ollama outage checks
```

Live suites consume rate-limit budget (10 logins/min/IP, 20-request `/ask` burst per user):
run them a minute apart or `docker compose restart paladin-api` between runs if you hit 429s.

## Known limitations & security roadmap

* **Aadhaar Verhoeff variant.** The detector implements a Verhoeff check with a 5-cycle
  position permutation; test vectors in `tests/test_pii_detection.py` validate against this
  implementation, not against the official UIDAI check digit. Real Aadhaar numbers may not
  match - treat detection as best-effort scrubbing, not authoritative validation.
* **PII coverage is regex-based.** No NER model: novel formats, non-Indian ID systems, and
  PII inside retrieved documents (as opposed to the user prompt) are not scrubbed.
  Roadmap: add an NER pass over both prompt and retrieved context.
* **No token revocation.** JWTs are stateless with no refresh tokens or revocation list;
  a leaked token is valid until `JWT_EXPIRY_MINUTES` elapses. Lower the expiry to shrink
  exposure; roadmap is short-lived access tokens plus a revocation/refresh mechanism.
* **Fixed signing key, no JWKS.** RSA keys are generated once by `scripts/generate_rsa_keys.sh`
  with no rotation. Roadmap: key rotation via JWKS.
* **Single-process rate limiter.** slowapi uses `storage_uri="memory://"`, so limits multiply
  with extra Uvicorn workers or API replicas. Use a shared Redis backend before scaling out.
* **Keyword injection guard.** 9 direct-injection patterns only; paraphrased or indirect
  (retrieved-document) injections are contained by context labelling, not detected.
* **No TLS in compose.** Traffic between browser and `:8000`/`:8501` is plain HTTP;
  terminate TLS in a reverse proxy for anything beyond localhost demos.
* **Single-model Ollama.** `OLLAMA_MAX_LOADED_MODELS=1`, `OLLAMA_NUM_PARALLEL=1`: concurrent
  `/ask` calls queue behind one CPU inference slot.

## OCI deployment guide (Always Free Ampere A1 ARM)

Target shape: the standard OCI Always Free Ampere A1 allocation is
**2 OCPUs / 12 GB RAM total per tenancy** (shared across all A1 instances
in the tenancy, not per instance). Anything larger (more OCPUs/RAM) only
applies if your specific OCI program or account entitlement explicitly
provides it - do not assume it.

Sizing model for PALADIN on a 12 GB A1 VM (leaving ~1-2 GB for the OS,
Docker engine, and page cache):

| Consumer | Budget | Notes |
| --- | --- | --- |
| Ollama (`llama3:8b` resident + context) | ~5-8 GB | `llama3:8b` needs ~4.7 GB resident before context; the compose cap is 8G |
| paladin-api (torch MiniLM + uvicorn) | ~1-2 GB | compose limit 2G |
| ChromaDB | ~0.5-1 GB | compose limit 1G |
| PostgreSQL | ~0.25-0.5 GB | compose limit 512M, `shared_buffers=128MB` |
| paladin-ui | ~0.25-0.5 GB | compose limit 512M |
| OS + Docker overhead | ~1-2 GB | must remain free; do not allocate it |

Do not apply 1 GB-VM-era per-service caps (e.g. 512M-everywhere) to this
stack - they will OOM Ollama and the embedding worker. The limits shipped
in `docker-compose.yml` are the OCI sizing; they assume the 12 GB A1 shape
above, not a 1 GB micro instance.

> **Limits are ceilings, not reservations.** The per-service `mem_limit` values
> sum to the 12 GB ceiling (2G + 512M + 512M + 1G + 8G = 12G) but cgroup limits
> do not reserve host RAM - a container only uses what it touches, and nothing
> is set aside for the OS. The sizing is safe because *observed* usage is far
> lower (Ollama peaks ~5.6 GiB under its 8G cap, the other four services settle
> at ~1 GB combined), which is what leaves the ~1-2 GB of real headroom for the
> OS, the Docker engine, and the page cache.

> **Measurement caveat.** Current memory measurements were taken on x86_64
> using the Docker Desktop VM. The OCI target is native ARM64 Linux. After
> deployment to the OCI VM, re-run the same `docker stats` + `ollama ps`
> measurements during model load and a representative RAG request, and
> confirm that the 8G Ollama limit remains safe on the real ARM64 target
> before treating the resource sizing as final.

1. Provision an Ampere A1 VM (Oracle Linux / Ubuntu 22.04) inside the
   2 OCPU / 12 GB tenancy budget. A 1 GB x86 micro instance cannot host
   this stack - `llama3:8b` alone exceeds it.
2. Install Docker + the compose plugin, copy the project, then:
   ```bash
   cd paladin
   cp .env.example .env          # set POSTGRES_PASSWORD; set OLLAMA_MODEL=llama3 for OCI/demo
   chmod +x scripts/*.sh
   ./scripts/generate_rsa_keys.sh
   docker compose up -d --build
   ./scripts/setup_ollama.sh   # pulls "$OLLAMA_MODEL"; large download (~4.7 GB for llama3)
   docker compose exec -T -e PYTHONPATH=/app paladin-api python data/ingest.py
   docker compose exec -T paladin-api python -m gateway.db.seed
   ```
   The stack must be running before `setup_ollama.sh` (it shells into the
   ollama container). Local development defaults to the small model
   (`OLLAMA_MODEL=llama3.2:3b` in `.env`); the deployed/demo instance uses
   `OLLAMA_MODEL=llama3`.
3. Verify: `http://<vm-ip>:8000/health`, `http://<vm-ip>:8000/docs`, `http://<vm-ip>:8501`
   (open 8000/8501 in the subnet security list; keep postgres/chromadb/ollama unexposed -
   compose already publishes no ports for them).
4. `docker compose ps` should show all five services healthy (Ollama reports healthy once
   the daemon answers `ollama list`, even before the model is pulled).

> **First boot on ARM64.** ARM64 support is unverified until tested on the target.
> On the OCI VM: (a) confirm multi-arch manifests with
> `docker manifest inspect --verbose <image>` for `postgres:16.15`,
> `chromadb/chroma:0.4.24` and `ollama/ollama:0.34.2` (expect `arm64` entries);
> (b) `docker compose build` once; (c) during model load and a representative
> RAG `/ask`, record `docker stats --no-stream` plus `docker compose exec -T ollama
> ollama ps` and confirm the Ollama container stays comfortably under its 8G cap
> with ~1-2 GB host headroom remaining before treating sizing as final.

