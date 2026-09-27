# Paladin - Implementation Plan

## Design Decisions (Confirmed)

| Decision            | Value                                                                          |
| ------------------- | ------------------------------------------------------------------------------ |
| Clearance levels    | 1=Employee, 2=Manager, 3=Admin                                                 |
| PII handling        | Redact PII and continue (log event)                                            |
| Rate limit          | 10 req/min sustained, burst 20, per authenticated user; slowapi `memory://` (single-process; does not scale across workers/replicas) |
| Chat history        | Persist to PostgreSQL (audit trail only, no conversation threading)            |
| Ollama model        | Env-driven: `llama3.2:3b` local default, `llama3` deployed/demo (`OLLAMA_MODEL` in `.env`) |
| JWT key management  | Auto-generate on first run via`scripts/generate_rsa_keys.sh`                 |
| ChromaDB filter     | Integer metadata:`clearance_level <= user_clearance_level`                   |
| Token counts        | Ollama native`prompt_eval_count` / `eval_count`                            |
| Users table columns | `id, username, password_hash, department, clearance_level, role, created_at` |
| Log tables          | `request_logs, denial_logs, pii_events, auth_failure_logs`                   |
| Demo users          | `emp1`(L1), `mgr1`(L2), `admin1`(L3) with known passwords                |
| Docker network      | Single internal network; expose FastAPI:8000 + Streamlit:8501 only             |

---

## Step 1: Infrastructure

**Goal:** Docker Compose with Postgres + FastAPI skeleton; verify connectivity.

### Files to Create

- `paladin/docker-compose.yml`
- `paladin/.env.example`
- `paladin/gateway/Dockerfile`
- `paladin/ui/Dockerfile`
- `paladin/gateway/main.py` (skeleton)

### docker-compose.yml

```yaml
services:
  paladin-api:
    build: ./gateway
    ports: ["8000:8000"]
    environment:
      - DATABASE_URL=postgresql+asyncpg://postgres:${POSTGRES_PASSWORD}@postgres:5432/paladin
      - JWT_PRIVATE_KEY_PATH=/app/keys/private.pem
      - JWT_PUBLIC_KEY_PATH=/app/keys/public.pem
      - CHROMADB_HOST=chromadb
      - OLLAMA_HOST=http://ollama:11434
      - REDIS_URL=redis://redis:6379
    depends_on: [postgres, chromadb, ollama]
    volumes:
      - ./keys:/app/keys  # RSA keys mounted
    networks: [internal]

  paladin-ui:
    build: ./ui
    ports: ["8501:8501"]
    environment:
      - API_URL=http://paladin-api:8000
    depends_on: [paladin-api]
    networks: [internal]

  postgres:
    image: postgres:16
    environment:
      POSTGRES_DB: paladin
      POSTGRES_USER: postgres
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
    volumes:
      - pgdata:/var/lib/postgresql/data
    networks: [internal]

  chromadb:
    image: chromadb/chroma:latest
    environment:
      - CHROMA_HOST=chromadb
      - CHROMA_PORT=8000
      - IS_PERSISTENT=true
      - CHROMA_COLLECTION_PERSIST_DIRECTORY=/chroma/data
    volumes:
      - chromadb_data:/chroma/data
    networks: [internal]

  ollama:
    image: ollama/ollama:latest
    volumes:
      - ollama_data:/root/.ollama
    networks: [internal]

  redis:
    image: redis:7-alpine
    networks: [internal]

networks:
  internal:
    driver: bridge

volumes:
  pgdata:
  chromadb_data:
  ollama_data:
```

### .env.example

```env
POSTGRES_PASSWORD=change_me_strong_password
JWT_ALGORITHM=RS256
JWT_EXPIRY_MINUTES=30
RATE_LIMIT_PER_MINUTE=10
RATE_LIMIT_BURST=20
```

### Verification

- `docker compose up` - all services healthy
- `curl localhost:8000/docs` - FastAPI Swagger loads
- Postgres connection from gateway container works

---

## Step 2: Database

**Goal:** SQLAlchemy models, session factory, seed script.

### Files to Create

- `paladin/gateway/db/models.py`
- `paladin/gateway/db/session.py`
- `paladin/gateway/db/seed.py`
- `paladin/data/ingest.py` (document ingestion)

### Schema Definition (models.py)

**users**

| Column          | Type        | Constraints         |
| --------------- | ----------- | ------------------- |
| id              | Integer     | PK, autoincrement   |
| username        | String(80)  | Unique, not null    |
| password_hash   | String(255) | Not null            |
| department      | String(80)  | Not null            |
| clearance_level | Integer     | Not null, check 1-3 |
| role            | String(40)  | Not null            |
| created_at      | DateTime    | Default now()       |

**request_logs**

| Column            | Type       | Constraints               |
| ----------------- | ---------- | ------------------------- |
| id                | Integer    | PK                        |
| request_id        | UUID       | Unique, not null          |
| user_id           | Integer    | FK users(id)              |
| department        | String(80) | From JWT                  |
| clearance_level   | Integer    | From JWT                  |
| sanitized_prompt  | Text       | Post-PII-redaction prompt |
| llm_response      | Text       | Full LLM response         |
| prompt_tokens     | Integer    | From Ollama               |
| completion_tokens | Integer    | From Ollama               |
| status            | String(20) | 'success'                 |
| created_at        | DateTime   | Default now()             |

**denial_logs**

| Column           | Type       | Constraints                                                          |
| ---------------- | ---------- | -------------------------------------------------------------------- |
| id               | Integer    | PK                                                                   |
| request_id       | UUID       | Unique                                                               |
| user_id          | Integer    | FK users(id)                                                         |
| department       | String(80) |                                                                      |
| clearance_level  | Integer    |                                                                      |
| reason_code      | String(40) | e.g., 'RATE_LIMIT_EXCEEDED', 'INSUFFICIENT_CLEARANCE', 'PII_BLOCKED' |
| reason_detail    | Text       | Human-readable                                                       |
| sanitized_prompt | Text       | Post-PII-redaction                                                   |
| created_at       | DateTime   |                                                                      |

**pii_events**

| Column       | Type        | Constraints                                       |
| ------------ | ----------- | ------------------------------------------------- |
| id           | Integer     | PK                                                |
| request_id   | UUID        |                                                   |
| user_id      | Integer     | FK users(id)                                      |
| pii_type     | String(40)  | 'CREDIT_CARD', 'AADHAAR', 'EMAIL', 'PHONE', 'SSN' |
| action       | String(20)  | 'REDACTED'                                        |
| masked_value | String(100) | e.g., '****-****-****-1234'           |
| created_at   | DateTime    |                                                   |

**auth_failure_logs**

| Column             | Type       | Constraints                                                               |
| ------------------ | ---------- | ------------------------------------------------------------------------- |
| id                 | Integer    | PK                                                                        |
| request_id         | UUID       |                                                                           |
| username_attempted | String(80) |                                                                           |
| failure_reason     | String(80) | 'INVALID_PASSWORD', 'MISSING_TOKEN', 'TOKEN_EXPIRED', 'INVALID_SIGNATURE' |
| ip_address         | String(45) |                                                                           |
| created_at         | DateTime   |                                                                           |

### Seed Users (seed.py)

```python
SEED_USERS = [
    {"username": "emp1", "password": "emp1_pass", "department": "Engineering", "clearance_level": 1, "role": "employee"},
    {"username": "mgr1", "password": "mgr1_pass", "department": "Engineering", "clearance_level": 2, "role": "manager"},
    {"username": "admin1", "password": "admin1_pass", "department": "Executive", "clearance_level": 3, "role": "admin"},
]
```

### Verification

- Run seed.py after DB is up
- Query each table to confirm rows exist
- Test login for each seeded user returns valid JWT

---

## Step 3: Authentication

**Goal:** RS256 JWT issuance, validation, auth routes.

### Files to Create

- `paladin/gateway/auth/jwt_utils.py` - Key loading, token creation/validation
- `paladin/gateway/auth/password_utils.py` - bcrypt hashing
- `paladin/gateway/routes/auth_routes.py` - `/auth/login`, `/auth/me`

### JWT Flow

1. **Login** (`POST /auth/login`): Accept `{username, password}` → verify against DB → issue RS256 JWT with claims `{sub: user_id, username, department, clearance_level, role, exp}`
2. **Token creation**: Load private key from `JWT_PRIVATE_KEY_PATH`, use `jose.jwt.encode()`
3. **Token validation** (middleware/decorator): Load public key from `JWT_PUBLIC_KEY_PATH`, use `jose.jwt.decode()`, verify algorithm=RS256, check expiration
4. **Token expiry**: 30 minutes (1800 seconds)

### Key Generation Script (scripts/generate_rsa_keys.sh)

```bash
#!/bin/bash
mkdir -p keys
openssl genrsa -out keys/private.pem 2048
openssl rsa -in keys/private.pem -pubout -out keys/public.pem
chmod 600 keys/private.pem
```

### Password Handling

- Use `passlib` with bcrypt
- Hash at seed time, never store plaintext

### Verification

- Login with emp1 → get JWT → decode with public key → verify claims
- Login with wrong password → 401
- Call protected endpoint without token → 401
- Call with expired/malformed token → 401

---

## Step 4: RAG Foundation

**Goal:** Document ingestion pipeline and clearance-filtered vector retrieval.

### Files to Create

- `paladin/data/ingest.py` - Read documents, chunk, embed, store in ChromaDB
- `paladin/gateway/rag/vector_store.py` - ChromaDB client, query with clearance filter
- `paladin/data/documents/` - Sample documents directory

### Document Ingestion Flow (ingest.py)

1. Read `.txt`/`.md` files from `data/documents/`
2. Split into chunks (max 512 tokens, 50% overlap)
3. For each chunk:
   - Generate embedding via `sentence-transformers` (`all-MiniLM-L6-v2`)
   - Store in ChromaDB with metadata: `{source_document, chunk_index, clearance_level, department}`
4. Log ingestion summary

### ChromaDB Collection Schema

- **Name**: `paladin_docs`
- **Metadata per chunk**: `source_document` (str), `chunk_index` (int), `clearance_level` (int), `department` (str)
- **Embedding dimension**: 384 (all-MiniLM-L6-v2)

### Vector Store Query (vector_store.py)

```python
def query(self, embedding: List[float], user_clearance: int, n_results: int = 5):
    results = collection.query(
        query_embeddings=[embedding],
        n_results=n_results,
        where={"clearance_level": {"$lte": user_clearance}}  # clearance filter
    )
    return results
```

### Verification

- Ingest sample documents → ChromaDB has expected chunk count
- Query with clearance 1 → only L1 documents returned
- Query with clearance 3 → all documents returned
- Ensure no documents leak across clearance boundaries

---

## Step 5: LLM Integration

**Goal:** Ollama client, query endpoint with full pipeline.

### Files to Create

- `paladin/gateway/llm/ollama_client.py` - Ollama API interaction
- `paladin/gateway/routes/query_routes.py` - `POST /ask` endpoint

### Ollama Client (ollama_client.py)

```python
class OllamaClient:
    def __init__(self, base_url: str, model: str = "llama3:8b-instruct"):
        self.base_url = base_url.rstrip("/")
        self.model = model

    def chat(self, system_prompt: str, context: str, user_query: str) -> dict:
        """Send to Ollama, return response + token counts."""
        # POST /api/chat with {model, messages: [system, context+query]}
        # Return: {response, prompt_eval_count, eval_count}
```

### Query Endpoint Flow (query_routes.py)

1. Receive `POST /ask` with `{message}` + Bearer token
2. **Security layer**: validate JWT → extract claims → assign correlation_id → check rate limit → PII detection/redaction
3. **RAG**: Embed cleaned prompt → query ChromaDB with clearance filter → build context string from top-k chunks
4. **Inference**: Call Ollama with system prompt + context + user query → get response + token counts
5. **Logging**: Insert into `request_logs` (with request_id, sanitized_prompt, response, token counts, user info)
6. **Audit log**: Insert into `pii_events` for any detected PII (redacted value only)
7. **Response**: Return `{response, sources: [document names], request_id}`

### Prompt Construction

```
System: You are Paladin, a secure enterprise knowledge assistant. 
        Answer based only on provided context. Cite sources.
Context: [RAG-retrieved chunks concatenated]
User: [Cleaned user query]
```

### Verification

- Login as emp1, ask a question about ingested documents → receive response
- Login as emp1, ask about manager-level documents → documents excluded from context
- Login as admin1, ask same question → documents included
- Ollama returns token counts → logged in DB
- Check request_logs table has correct entries

---

## Step 6: Security & Middleware

**Goal:** PII detection, rate limiting, request ID, logging verification.

### Files to Create

- `paladin/gateway/security/pii_detector.py` - Regex + checksum validators
- `paladin/gateway/security/rate_limiter.py` - slowapi integration
- `paladin/gateway/middleware/correlation_middleware.py` - Request ID injection
- `paladin/gateway/middleware/auth_middleware.py` - JWT validation

### PII Detection (pii_detector.py)

**Detection Rules (regex-based only, no NER):**

| PII Type    | Pattern                                            | Validation               |
| ----------- | -------------------------------------------------- | ------------------------ |
| Credit Card | `\b(?:\d[ -]?){13,19}\b`                         | Luhn algorithm check     |
| Aadhaar     | `\b\d{4}\s?\d{4}\s?\d{4}\b`                      | Verhoeff algorithm check |
| Email       | `[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}` | Format check             |
| Phone (IN)  | `\b\d{10}\b`                                     | Length check             |
| SSN         | `\b\d{3}-\d{2}-\d{4}\b`                          | Format check             |

**Actions:**

- Credit Card: REDACT → `****-****-****-1234`
- Aadhaar: REDACT → `****-****-****`
- Email: REDACT → `****@****.***`
- Phone: REDACT → `**** ****`
- SSN: REDACT → `***-**-****`

**Key constraint**: Raw PII never written to DB. Only masked_value + pii_type + request_id stored in `pii_events`.

### Rate Limiting (rate_limiter.py)

- Use `slowapi` with `RateLimiter`
- Storage: Redis (in-memory fallback for dev)
- Key: `rate_limit:{user_id}` from JWT
- Limits: 10/min normal, 20 burst
- Response: 429 Too Many Requests with `Retry-After` header

### Correlation ID Middleware

- Generate UUID v4 for each request
- Store in `X-Request-ID` header and response header
- Log correlation_id in all DB entries for traceability

### Verification

- Send request with credit card number → Response has `[REDACTED]`, pii_events table has entry, request_logs has sanitized prompt (no raw card number)
- Send 11th request within 1 minute → 429
- Check every request has a unique correlation_id

---

## Step 7: Frontend

**Goal:** Streamlit chat UI with login and chat interface.

### Files to Create

- `paladin/ui/app.py` - Main Streamlit application
- `paladin/ui/utils/api_client.py` - HTTP client for FastAPI

### UI Components (app.py)

1. **Login page**: Username/password form → POST `/auth/login` → store JWT in session state
2. **Chat page** (authenticated only):
   - Message history in session state (persisted to DB via API)
   - Input box → POST `/ask` with Bearer token
   - Display response with source citations
   - Show request_id for audit
3. **Logout**: Clear session state JWT

### API Client (api_client.py)

```python
class PaladinClient:
    def __init__(self, base_url, token=None):
        self.base_url = base_url
        self.token = token
        self.headers = {"Authorization": f"Bearer {token}"} if token else {}

    def login(self, username, password): ...
    def get_me(self): ...
    def ask(self, message): ...
    def get_metrics(self): ...  # admin only
```

### Verification

- Login as emp1 → Chat page accessible
- Ask question → Response displayed with sources
- Login as admin1 → Can access `/metrics/stats` (NOT `/metrics/summary`)
- Token expiry → Redirect to login

---

## Step 8: Finalization

**Goal:** Admin metrics, targeted tests, professional README.

### Files to Create

- `paladin/gateway/routes/metrics_routes.py` - `GET /metrics/stats` (admin only; NOT `/metrics/summary`)
- `paladin/tests/test_jwt_security.py`
- `paladin/tests/test_authorization.py`
- `paladin/tests/test_pii_detection.py`
- `paladin/tests/test_rate_limit.py`
- `paladin/README.md`

### Admin Metrics (metrics_routes.py)

**Endpoint:** `GET /metrics/stats` (requires role=admin; the `/metrics/summary` path in earlier drafts was never implemented)

**Metrics (aggregate SQL queries):**

| Metric                | SQL                                                                                |
| --------------------- | ---------------------------------------------------------------------------------- |
| Requests by dept      | `SELECT department, COUNT(*) FROM request_logs GROUP BY department`              |
| Total tokens consumed | `SELECT SUM(prompt_tokens) + SUM(completion_tokens) FROM request_logs`           |
| Blocks by reason      | `SELECT reason_code, COUNT(*) FROM denial_logs GROUP BY reason_code`             |
| PII events by type    | `SELECT pii_type, COUNT(*) FROM pii_events GROUP BY pii_type`                    |
| Auth failures         | `SELECT failure_reason, COUNT(*) FROM auth_failure_logs GROUP BY failure_reason` |

### Tests

**test_jwt_security.py:**

- Valid login returns RS256 JWT
- JWT contains required claims (sub, dept, clearance, role)
- Expired token rejected
- Tampered token rejected
- Token without required claims rejected

**test_authorization.py:**

- emp1 cannot access admin routes
- emp1 cannot see mgr1's department docs (clearance check)
- admin1 can access all docs and metrics
- `/ask` without token → 401

**test_pii_detection.py:**

- Credit card number (valid Luhn) → redacted
- Invalid credit card (fails Luhn) → not flagged
- Valid Aadhaar (passes Verhoeff) → redacted
- Invalid Aadhaar (fails Verhoeff) → not flagged
- Email detected → redacted
- Raw PII not in any DB entry after redaction

**test_rate_limit.py:**

- 10th request within 1 min → 200
- 11th request within 1 min → 429
- After 1 minute → requests succeed again

### README.md

Sections:

1. Overview & Architecture Diagram
2. Quick Start (Docker Compose)
3. Demo Credentials Table
4. API Reference (Swagger link)
5. Security Features
6. Known Limitations & Security Roadmap (JWKS, revocation, prompt injection, NER)
7. Testing
8. OCI Deployment Guide

---

## Step 9: Deployment

**Goal:** Docker Compose optimized for OCI Always Free ARM VM.

### Key Considerations

- **ARM compatibility**: All images must support `linux/arm64` (Postgres 16.15, Ollama 0.34.2, ChromaDB 0.4.24 expected; UNVERIFIED on target as of 2026-09-27 — manifest + native build checks outstanding)
- **Memory limits**: Standard OCI Ampere A1 = 2 OCPUs / 12 GB per tenancy (NOT 1 GB). Enforced `mem_limit`/`cpus` per service (plain `docker compose up` ignores `deploy.resources`); Ollama capped at 8G/2.0 CPUs. Limits are ceilings, not reservations — safety comes from observed usage, and ARM64 re-measurement is outstanding.
- **Network isolation**: Internal network only, no published ports for backend services
- **Persistence**: Named volumes for Postgres, ChromaDB, Ollama data

### Resource Constraints (docker-compose.yml additions)

```yaml
deploy:
  resources:
    limits:
      memory: 8G  # ollama; api 2G, chroma 1G, postgres/ui 512M each (see docker-compose.yml)
# NOTE: plain `docker compose up` IGNORES deploy.resources — the stack duplicates
# every cap as top-level mem_limit/mem_reservation/cpus, which the engine enforces.
```

### deploy_oci.md

Steps:

1. Provision OCI VM (Ampere A1 ARM64, Oracle Linux/Ubuntu 22.04, within the 2 OCPU / 12 GB tenancy budget — NOT a 1 GB micro instance)
2. Install Docker & Docker Compose plugin, bash, openssl
3. Copy project (private transfer only); create fresh `.env` (`OLLAMA_MODEL=llama3` for demo) + fresh RSA keys; NEVER copy `.env`/`keys/` between machines
4. `docker compose up -d`
5. Verify ports 8000 (API) + 8501 (UI) accessible; postgres/chromadb/ollama publish no ports
6. Pull `llama3` in Ollama container via `./scripts/setup_ollama.sh` (large ~4.7 GB download)

---

## File Inventory (Complete)

### Root

```
paladin/
├── docker-compose.yml
├── .env.example
├── README.md
└── paladin-plan.md (this plan)
```

### gateway/

```
gateway/
├── Dockerfile
├── main.py
├── config.py
├── auth/
│   ├── __init__.py
│   ├── jwt_utils.py
│   └── password_utils.py
├── security/
│   ├── __init__.py
│   ├── pii_detector.py
│   └── rate_limiter.py
├── rag/
│   ├── __init__.py
│   └── vector_store.py
├── llm/
│   ├── __init__.py
│   └── ollama_client.py
├── db/
│   ├── __init__.py
│   ├── models.py
│   ├── session.py
│   └── seed.py
├── middleware/
│   ├── __init__.py
│   ├── correlation_middleware.py
│   └── auth_middleware.py
└── routes/
    ├── __init__.py
    ├── auth_routes.py
    ├── query_routes.py
    └── metrics_routes.py
```

### ui/

```
ui/
├── Dockerfile
├── app.py
└── utils/
    └── api_client.py
```

### data/

```
data/
├── documents/
│   ├── engineering_policy.txt
│   ├── hr_handbook.md
│   ├── finance_guidelines.txt
│   └── executive_briefing.md
└── ingest.py
```

### tests/

```
tests/
├── __init__.py
├── test_jwt_security.py
├── test_authorization.py
├── test_pii_detection.py
└── test_rate_limit.py
```

### scripts/

```
scripts/
├── generate_rsa_keys.sh
├── setup_ollama.sh
└── deploy_oci.md
```

---

## Execution Order & Verification Gates

| Step              | Gate Criteria                                                       |
| ----------------- | ------------------------------------------------------------------- |
| 1. Infrastructure | `docker compose up` → FastAPI Swagger accessible on :8000        |
| 2. Database       | Seed users queryable; all 5 tables exist with columns               |
| 3. Auth           | Login → valid JWT; protected routes enforce auth                   |
| 4. RAG            | Documents ingested; clearance filter works correctly                |
| 5. LLM            | `/ask` returns responses; token counts logged; clearance enforced |
| 6. Security       | PII redacted; rate limit enforced; correlation IDs present          |
| 7. Frontend       | Streamlit chat working end-to-end with login                        |
| 8. Finalization   | Unit suite + available live suites run; report written; README complete      |
| 9. Deployment     | OCI deploy guide corrected; ARM64 build + stats/ps re-measurement UNVERIFIED |

## Verification Status (2026-09-27 follow-up — see paladin/test-reports/2026-09-27-phase6-closing.md)

Fresh results from the Phase 6 follow-up pass (local x86_64 Docker Desktop only).
The earlier `2026-09-27-verification.md` is SUPERSEDED by the closing report.

- **Correction to earlier records:** `tests/test_integration_live.py` is a
  *script* (`def main()`, no `unittest.TestCase` classes). Running
  `python -m unittest tests.test_integration_live` reports
  `Ran 0 tests / NO TESTS RAN` and executes **no** integration checks. Full
  count breakdown:

| Figure | Suites actually included | Notes |
| --- | --- | --- |
| "21/21 combined" | `test_pii_detection` + `test_ask_failure_live` | predates `test_ask_http_503_live.py` (contributed 0); `test_integration_live` contributed 0 |
| "23/23 combined" | + `test_ask_http_503_live` | first run including the ASGI-level suite |
| "25/25 combined" (current) | same three suites | after adding 2 initialization-failure tests |
| "integration 33/33" | **not a unittest run** | separately invoked script `python tests/test_integration_live.py --fast --no-disruption`; prior result, not re-executed |

Unittest counts and integration-script results are never summed or presented as
the same class of evidence; historical figures are labelled as prior results.
- Offline PII unit suite: PASS (17 host / 19 in-container).
- Failure-path regression tests: PASS (4/4 `test_ask_failure_live`, direct
  handler — query, constructor-init and embedding-model-init failures; 2/2
  `test_ask_http_503_live`, full ASGI app -> real HTTP 503).
- Combined focused unit run: **25/25 OK**.
- Downstream-init fix: `VectorStore()` / `get_embedding_model()` moved inside
  the guarded block, and model-load failures now convert to a typed
  `ServiceUnavailableError("embedding model")`, so no downstream-init path can
  return 500 or orphan the already-committed PII rows.
- Live rate-limit/token suite: **PASS 21/21** (rerun on a populated
  collection; `documents_retrieved=4` for admin1, logged `prompt_tokens=350`
  cross-checked equal to a direct `OllamaClient().chat()` `prompt_eval_count`
  of 350, and 1-doc emp1=162 < 4-doc admin1=350).
- Live e2e suite (`tests/test_ask_e2e_live.py`): **PASS** — HTTP 200,
  `Sources: ['engineering_policy.txt']`, answer grounded in that document,
  audit row `status=success, prompt_tokens=156, completion_tokens=89`,
  joined to `users` as **username `emp1`, clearance_level 1** (exactly one
  matching row; consistent with the script authenticating as `emp1`).
- RAG data: Chroma `count()=4` (one chunk per document, clearance 1/2/2/3);
  clearance-filtered retrieval returns 1/3/4 documents for levels 1/2/3.
- Chroma persistence across `docker compose up -d --force-recreate --no-deps
  chromadb`: volume `paladin_chromadb_data` preserved (unchanged
  `created=2026-09-22T17:01:08Z`), count still 4, IDs unchanged, live `/ask`
  still served afterwards.
- Historical integration/auth results above stand as previously recorded but
  were **not re-executed** in this pass (out of scope by instruction).
- Disruptive outage checks, OCI ARM64 build/manifests, and `docker stats` +
  `ollama ps` sign-off on the real target: **OUTSTANDING**. No deployment
  readiness is claimed. Verdict remains **local-only**.
