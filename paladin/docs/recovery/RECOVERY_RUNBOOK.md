# Paladin Recovery Runbook — Versioned-Generation Vector Index

**Applies to:** Paladin gateway with versioned Chroma generation collections  
**PostgreSQL:** 16  
**ChromaDB:** 0.4.24 (pinned)  
**Ollama:** 0.34.2  
**Status:** Authoritative procedures reconstructed from source code and two isolated rehearsals  
**Evidence limit:** Procedures below mix three evidence types. Where a procedure was exercised during the isolated rehearsals (`pgvr-ih9ae8`, `20261019-191425-559672`), it is stated explicitly. Where a procedure was verified only by source-code inspection of `data/ingest.py`, `gateway/rag/generations.py`, `gateway/db/models.py`, or `paladin/docker-compose.yml`, it is marked **source-verified**. Where behavior has neither been exercised nor source-verified, it is marked **unverified**. **No backup, restore, rollback, ingestion, or deployment validation has ever been performed against the live Paladin stack.**

---

## 0. Disclaimers and Safety Boundaries

### 0.1 Disposable vs. Live Operations

Recovery procedures were exercised across two isolated rehearsals against disposable stacks (private Docker networks, no host ports bound):

- **Rehearsal `pgvr-ih9ae8`**: Verified single-generation ingestion, pointer activation, PostgreSQL backup/restore (`pg_dump`/`pg_restore`), Chroma backup/restore via `tar` volume copy, rollback refusal due to no previous generation existing, and cleanup.
- **Rehearsal `20261019-191425-559672`**: Verified two-generation ingestion/cutover, active-pointer resolution, successful Gen-2 → Gen-1 rollback, hash-failure refusal, and cleanup.

These results were obtained from disposable, isolated rehearsal stacks. No deleted temporary rehearsal reports or workspaces were independently re-opened or re-verified.

**No backup, restore, rollback, ingestion, or deployment validation has ever been performed against the live Paladin stack.**

Before running any procedure against live infrastructure, obtain explicit change-management approval and verify that the target environment matches the code version described here.

### 0.2 Demo Credentials

The seed script (`gateway/db/seed.py`) creates four demo user accounts with hardcoded demonstration passwords. **These credentials must not be used for any public or production deployment.** The runbook references user seeding only by username and role; it never records or requires passwords.

### 0.3 What This Runbook Does Not Cover

- Live-production backup or restore (never performed)
- Cross-environment migration of generation state
- ChromaDB versions other than 0.4.24
- PostgreSQL versions other than 16

---

## 1. Prerequisites

### 1.1 Safety Isolation Requirements

Before operating against any non-disposable environment:

1. Confirm the target Compose project name is **not** `paladin` unless live operations are explicitly authorized.
2. Confirm no host ports are bound to the target stack unless required for the specific procedure.
3. Verify a private Docker network isolates the stack from other workloads.
4. Confirm database connection credentials are sourced from environment variables or a secrets manager — never from command history or shell redirection.

### 1.2 User Seeding Before API Smoke Tests

The API smoke test (`/auth/login` + `/ask`) requires at least one valid database user. The `users` table is created by SQLAlchemy model metadata (`Base.metadata.create_all`) during application startup, but is **empty** after a fresh database initialization.

**Authentication error distinction:**
- **Empty `users` table (no matching row):** `/auth/login` returns **HTTP 401** (invalid credentials — user not found). This is not a dependency failure.
- **Missing `users` table or database unreachable:** `/auth/login` returns **HTTP 503** (dependency unavailable — SQLAlchemy cannot query a table that does not exist, or the database connection itself failed).

Seeding is a prerequisite before smoke tests because an empty table produces 401 errors that would be mistaken for authentication failures rather than missing seed data.

**Procedure:**

```bash
# Run inside the paladin-api service container with PYTHONPATH set
docker compose exec -T -e PYTHONPATH=/app paladin-api python gateway/db/seed.py
```

**Expected output:**
```
[SEED] Created user: emp1 (clearance: 1, role: employee)
[SEED] Created user: mgr1 (clearance: 2, role: manager)
[SEED] Created user: admin1 (clearance: 3, role: admin)
[SEED] Created user: fin1 (clearance: 2, role: employee)
[SEED] Seeding complete. 4 created, 0 already existed.
```

**Notes:**
- The script is idempotent: re-running skips existing usernames and preserves their password hashes.
- Do not modify `seed.py` to change demo passwords. The script is checked into the repository with hardcoded demonstration credentials.
- **source-verified:** The seeding behavior (idempotent skip of existing usernames) is confirmed in `gateway/db/seed.py`.
- **unverified:** Whether seeding is required before every smoke test against a fresh live database has not been confirmed against production.

---

## 2. Ingestion Logic

### 2.1 Generation-Aware Ingestion Overview

Ingestion is implemented in `paladin/data/ingest.py`. The orchestration logic (`GenerationStore`) lives in `paladin/gateway/rag/generations.py`. The correct execution sequence is:

1. **Verify document bytes:** The `DocumentIngestor` reads each document file, computes its SHA-256, and validates document bytes against the manifest (`paladin/data/manifest.json`). Invalid manifests raise `ManifestError`.
2. **Acquire advisory lock + pointer-table setup/legacy bootstrap:** `GenerationStore.acquire()` acquires a session-level PostgreSQL advisory lock (key `0x50414C47`, ASCII "PALG") to serialize concurrent ingestions. It then ensures the partial unique index `vector_generations_one_active` exists (`CREATE UNIQUE INDEX ... ON vector_generations (is_active) WHERE is_active`). On a fresh install with no pointer rows, `bootstrap_legacy()` adopts the pre-versioning legacy collection as the active generation — but only if that collection exists in Chroma (verified via the `collection_exists` probe); otherwise it returns `None` and the first versioned ingestion builds `gen_000001` directly.
3. **Register and build the new generation:** A new versioned Chroma collection named `<CHROMA_COLLECTION>_gen_<NNNNNN>` is created and populated (embedded and chunked). A corresponding row is inserted into `vector_generations` with `is_active=false`.
4. **Validate, activate the pointer, cleanup, and release the lock:** The new generation is validated. If validation passes, `activate()` flips the `is_active` pointer to the new generation in a single transaction (updating the old row to `is_active=false` with `deactivated_at` set, and inserting/updating the new row to `is_active=true` with `activated_at` set). The advisory lock is then released via `GenerationStore.release()`.

**Pointer flip is atomic:** The `is_active` flip occurs in a single PostgreSQL transaction. Queries always resolve to exactly one fully built generation via `VectorStore` (`paladin/gateway/rag/vector_store.py`), which reads the pointer on every `/ask` request. The most recently deactivated generation is retained as the rollback target.

**Important:** Registration and building of the new generation must **not** be described as occurring before lock acquisition. The advisory lock is acquired first, then pointer-table setup/bootstrap occurs, then the new generation is registered and built.

### 2.2 Pre-Ingestion Pointer-Table Preflight

Before ingestion, check the state of the `vector_generations` table in PostgreSQL:

**Step 1 — Check if the table exists:**

```sql
SELECT to_regclass('vector_generations');
```

- Returns `NULL` → table is missing (pre-ingestion / fresh install). Ingestion will perform legacy bootstrap if a legacy Chroma collection exists, or proceed directly to versioned generation `gen_000001`.
- Returns `vector_generations` → table exists; proceed to Step 2.

**Step 2 — Check active pointer state:**

```sql
SELECT name, is_active, activated_at, deactivated_at
FROM vector_generations
ORDER BY name;
```

Interpret the results:

| Result | Meaning | Action |
|--------|---------|--------|
| Zero rows | Table exists but is empty | Fresh install; ingestion will bootstrap legacy or create `gen_000001` |
| Exactly one row with `is_active = true` | Healthy single active generation | Proceed with ingestion |
| More than one row with `is_active = true` | **Corrupt pointer state** | **Do not ingest**; investigate before proceeding |
| Rows exist but none with `is_active = true` | **Corrupt pointer state** | **Do not ingest**; investigate before proceeding |

### 2.3 Running Ingestion

```bash
docker compose exec -T -e PYTHONPATH=/app paladin-api python data/ingest.py
```

**Expected output on success:**
```
ingested 4 chunks into the active generation
```

**Exit codes:**
- `0` — ingestion succeeded, pointer flipped
- `1` — ingestion refused (`GenerationError` or `ManifestError`); pointer untouched

### 2.4 Post-Ingestion Verification

```sql
-- Confirm exactly one active generation
SELECT count(*) AS active_count FROM vector_generations WHERE is_active = true;
-- Expected: 1

-- Confirm the active generation name
SELECT name FROM vector_generations WHERE is_active = true;
```

Then probe Chroma:
```python
import chromadb
client = chromadb.HttpClient(host="chromadb", port=8000)
collections = sorted(c.name for c in client.list_collections())
print(collections)
# Expected: includes the collection matching the active pointer row
# e.g. ['paladin_docs_abac_gen_000001']
```

**source-verified:** The ingestion sequence (lock → bootstrap → register/build → validate → activate → release) and the advisory lock key `0x50414C47` are confirmed in `paladin/gateway/rag/generations.py` and `paladin/data/ingest.py`.

---

## 3. PostgreSQL 16 — Custom-Format Backup and Restore

### 3.1 Backup (pg_dump)

Use PostgreSQL's custom-format dump (`-Fc`) for binary-safe, compressible backups:

```bash
pg_dump -U postgres -d paladin -Fc -f paladin_backup.dump
```

**Windows PowerShell critical warning:** Do **not** redirect `pg_dump` output with `>` in PowerShell. PowerShell's text redirection transcodes the binary dump stream as UTF-16 LE (BOM `0xFF 0xFE`), which corrupts the dump and causes `pg_restore` to fail with `invalid byte sequence for encoding "UTF8": 0xff`.

**Correct approaches:**

Option A — Write directly to file with `-f` (recommended):
```bash
pg_dump -U postgres -d paladin -Fc -f /path/to/paladin_backup.dump
```

Option B — Execute inside the container and copy the file out:
```bash
docker compose exec -T postgres pg_dump -U postgres -d paladin -Fc -f /tmp/paladin_backup.dump
docker cp <container>:/tmp/paladin_backup.dump ./backups/paladin_backup.dump
```

### 3.2 Restore (pg_restore)

**PostgreSQL 16 syntax:** The archive is provided as a positional filename argument. The `-f` flag is **not** an input-file flag for `pg_restore` (it is for writing SQL output to a file). Do not use `-f` to specify the input archive.

```bash
pg_restore -U postgres -d paladin -v paladin_backup.dump
```

**Windows PowerShell critical warning:** Do **not** pipe a dump file into `psql` or `pg_restore` using shell input redirection (`<`). Use a file argument instead.

**Restore into a separate instance:**
1. Start a second PostgreSQL 16 container with an empty `paladin` database.
2. Copy the dump file into that container.
3. Run `pg_restore -U postgres -d paladin -v /tmp/paladin_backup.dump`.
4. Verify the restored `vector_generations` table contains the expected active pointer row.

**source-verified:** pg_restore positional filename syntax is documented at https://www.postgresql.org/docs/16/app-pgrestore.html.
**unverified:** Whether `pg_restore` against a live production database preserves Chroma collection-pointer consistency has not been tested.

---

## 4. ChromaDB 0.4.24 — Backup and Restore

### 4.1 Architecture Notes

ChromaDB 0.4.24 offers **no atomic rename, alias, or swap primitive** for collections. The Paladin architecture compensates by using PostgreSQL `vector_generations` as the authoritative pointer: ingestion builds a new collection while the old one remains pointed-at, then flips the pointer atomically in PostgreSQL.

**Chroma pathing (source-verified from `paladin/docker-compose.yml`):**
- The `chromadb` service sets `PERSIST_DIRECTORY=/chroma/chroma` via environment variable.
- The named volume `chromadb_data` is mounted at `/chroma/chroma` inside the service container: `chromadb_data:/chroma/chroma`.
- **Do not state specific SQLite file paths** (e.g., `chroma.sqlite3`) unless explicitly confirmed by the project configuration; the internal file layout of ChromaDB 0.4.24 is an implementation detail.

When backing up or restoring via a temporary helper container, distinguish between:
- **Service mount destination:** `/chroma/chroma` (inside the `chromadb` service container)
- **Helper container mount path:** arbitrary path chosen for the helper container (e.g., `/source` or `/target`)

### 4.2 Backup

1. **Stop the Chroma service** (or quiesce writes) to ensure a consistent on-disk state:
   ```bash
   docker compose stop chromadb
   ```
2. **Copy the persisted data volume** to a backup location (mount the `chromadb_data` volume read-only at an arbitrary helper-container path):
   ```bash
   docker run --rm \
     -v chromadb_data:/source:ro \
     -v <local_backup_dir>:/target \
     alpine sh -c 'tar czf /target/chroma_backup.tar.gz -C /source .'
   ```
3. **Restart Chroma:**
   ```bash
   docker compose start chromadb
   ```

**Service stop window:** Chroma must be stopped for the duration of the volume copy. The window size depends on volume size.

### 4.3 Restore

1. **Stop the target Chroma service:**
   ```bash
   docker compose stop chromadb
   ```
2. **Extract the backup into the target volume** (mount the `chromadb_data` volume at an arbitrary helper-container path):
   ```bash
   docker run --rm \
     -v chromadb_data:/target \
     -v <local_backup_dir>:/source \
     alpine sh -c 'tar xzf /source/chroma_backup.tar.gz -C /target'
   ```
3. **Restart Chroma:**
   ```bash
   docker compose start chromadb
   ```
4. **Verify restoration:**
   ```python
   import chromadb
   client = chromadb.HttpClient(host="chromadb", port=8000)
   collections = sorted(c.name for c in client.list_collections())
   print(collections)
   # Expected: includes the collection matching the active pointer row
   ```

### 4.4 Restart Protocol on Copy Failure

If the `tar` extraction fails:

1. Do **not** restart Chroma with a partially extracted volume.
2. Remove the target volume and recreate it empty.
3. Re-attempt extraction from the backup archive.
4. If extraction fails a second time, the backup archive may be corrupt — obtain a fresh backup and repeat from step 1.
5. Only after successful extraction and verification, restart Chroma.

**unverified:** ChromaDB 0.4.24 does not document a formal `VACUUM` or consistency-check command for the on-disk SQLite backend. Whether a restored Chroma instance requires a consistency check before serving queries is unverified.

---

## 5. Rollback Scenarios

### 5.1 Successful Rollback (Previous Generation Exists)

When at least two generations exist and the most recently deactivated generation's Chroma collection is still present:

```bash
docker compose exec -T -e PYTHONPATH=/app paladin-api python data/ingest.py --rollback
```

**Internal behavior (`paladin/gateway/rag/generations.py` — `GenerationStore.rollback`):**
1. Calls `previous_name()` to identify the most recently deactivated generation (ordered by `deactivated_at`).
2. Verifies the target's Chroma collection exists via `collection_exists` probe.
3. Calls `activate(previous)` to flip the `is_active` pointer in a single transaction.
4. Returns the rolled-back generation name.

**Expected output:**
```
rolled back to <previous_generation>
```

**Rollback target note:** The most recently deactivated generation is the rollback target. Following the first versioned cutover, this may be the legacy (pre-versioning) collection that was adopted as the initial active generation via `bootstrap_legacy()`.

### 5.2 Fail-Closed Refusal (No Rollback Target)

When only one generation exists (or no previous deactivated generation is registered):

```bash
docker compose exec -T -e PYTHONPATH=/app paladin-api python data/ingest.py --rollback
```

**Expected output:**
```
ingestion refused: no previous generation to roll back to
```
**Exit code:** 1

**Pointer state:** Untouched. The active generation remains serving. This is correct fail-closed behavior — the operator must not be left with an ambiguous pointer state.

**source-verified:** The rollback refusal message and exit code are confirmed in `paladin/data/ingest.py` (main function catches `GenerationError` and prints `ingestion refused: {exc}` with return code 1) and `paladin/gateway/rag/generations.py` (`GenerationStore.rollback` raises `GenerationError("no previous generation to roll back to")` when `previous_name()` returns `None`).

### 5.3 Rollback Target Collection Missing

If the previous generation exists in PostgreSQL but its Chroma collection has been deleted:

```bash
docker compose exec -T -e PYTHONPATH=/app paladin-api python data/ingest.py --rollback
```

**Expected output:**
```
ingestion refused: refusing to roll back to <name>: its collection is missing from Chroma; restore the collection first
```
**Exit code:** 1

**Required action:** Restore the missing Chroma collection from backup (Section 4), then retry rollback.

### 5.4 Corrupt Pointer State

If `vector_generations` contains zero active rows or more than one active row:

1. **Do not ingest or rollback.**
2. Investigate the partial unique index `vector_generations_one_active` (`CREATE UNIQUE INDEX ... ON vector_generations (is_active) WHERE is_active`).
3. Manually reconcile to exactly one active row, or restore PostgreSQL from backup.

**unverified:** Manual pointer reconciliation procedure against a live database has not been tested.

---

## 6. Error Handling

### 6.1 Pre-Pointer-Activation Failures

| Failure | Symptom | Response |
|---------|---------|----------|
| Chroma unreachable | `ValueError("Could not connect to a Chroma server...")` | Fail closed: do not ingest; verify Chroma container and network |
| Missing collection error misidentified | Treating unreachable Chroma as "collection absent" | **Never** do this — it would cause adoption of a nonexistent legacy collection. Identify missing collections only by the exact driver-error signature `collection <name> does not exist`. |
| `users` table empty (no matching row) | `/auth/login` returns HTTP 401 (invalid credentials — user not found); not a dependency failure | Seed users first (Section 1.2) |
| Missing `users` table or DB unreachable | `/auth/login` returns HTTP 503 (dependency unavailable) | Verify database is running and migrated |
| Manifest invalid | `ManifestError` from `ingest.py` | Fix `paladin/data/manifest.json` or document set; retry |

### 6.2 Post-Pointer-Activation Failures

| Failure | Symptom | Response |
|---------|---------|----------|
| New collection built but pointer flip failed | New generation registered but not active | Old generation still serving; new collection is orphaned. Retry ingestion or run cleanup (`--cleanup`). |
| Rollback target collection missing | Rollback refused with exit 1 | Restore Chroma collection from backup, then retry rollback |
| More than one active row | `read_active_generation` rejects corrupt state | Do not ingest; restore PostgreSQL from backup and reconcile |

### 6.3 Cleanup of Out-of-Retention Generations

```bash
docker compose exec -T -e PYTHONPATH=/app paladin-api python data/ingest.py --cleanup
```

Removes generations older than the retention window (default `IN_USE_GRACE_SECONDS = 900` seconds since deactivation). Never removes the active generation or generations deactivated less than the grace period ago. Best-effort: failed deletions skip the row and retry on the next run.

**unverified:** The default 900-second grace period has not been validated against production request latencies.

---

## 7. Cleanup

### 7.1 Targeted Rehearsal Resource Removal

Remove only resources belonging to a specific rehearsal project. **Never** use `docker system prune`.

```bash
# 1. Remove rehearsal containers
docker rm <rehearsal-container-1> <rehearsal-container-2> ...

# 2. Remove rehearsal network
docker network rm <rehearsal-network-name>

# 3. Remove rehearsal volumes
docker volume rm <rehearsal-volume-1> <rehearsal-volume-2> ...
```

Identify rehearsal resources by Compose project label:
```
com.docker.compose.project=<rehearsal-project-name>
```

### 7.2 Rehearsal Image Removal

Remove only uniquely tagged rehearsal images:

```bash
docker rmi <rehearsal-image>:<tag>
```

**Prohibitions:**
- Do not use `--force`.
- Do not remove images used by live containers.
- Do not remove base images (`postgres:16.15`, `chromadb/chroma:0.4.24`, `ollama/ollama:0.34.2`, etc.).

**Verification before removal:**
```bash
# Confirm no containers reference the image
docker ps -a --filter 'ancestor=<rehearsal-image>:<tag>'

# Confirm the image is not the live API image
docker images --filter 'reference=paladin-paladin-api*'
```

**unverified:** Whether image removal frees disk space immediately (vs. on daemon restart) has not been confirmed.

---

## 8. Authoritative References

| Component | Version | Documentation |
|-----------|---------|---------------|
| PostgreSQL | 16 | https://www.postgresql.org/docs/16/ |
| ChromaDB | 0.4.24 | https://docs.trychroma.com/ |
| SQLAlchemy | — | https://docs.sqlalchemy.org/ |
| Ollama | 0.34.2 | https://github.com/ollama/ollama |

---

## 9. Revision History

| Version | Date | Change |
|---------|------|--------|
| 1.1 | 2026-10-19 | Documentation-only corrections: pg_restore syntax, paladin-api service name, authentication error logic, ingestion workflow order, Chroma pathing, rollback output placeholder, pointer-table preflight, evidence refinement |

---

**End of runbook.** This document is intended for operational use by the Paladin SRE team. It must be reviewed and updated whenever `data/ingest.py`, `gateway/rag/generations.py`, or `gateway/db/models.py` changes.