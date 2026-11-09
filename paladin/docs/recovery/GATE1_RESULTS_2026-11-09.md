# Gate 1 Results — Live-Source Backup and Disposable Restore

**Date:** 2026-11-09

**Result:** **PASS within the scope below**

**Scope:** Back up only the live Paladin PostgreSQL database and Chroma data volume, restore both into disposable isolated services, and verify restored data with the 16-case ABAC policy and retrieval-filter matrix.

## Results

### PostgreSQL

- Created a PostgreSQL custom-format (`pg_dump -Fc`) backup of database `paladin` only.
- Archive: 45,521 bytes; 41 archive entries; SHA-256 `1BEAB635F72568A7006BC1921C168A05900BD0CE9A5C730C84A1484DD98D57E2`.
- `pg_restore --list` succeeded, and the archive restored into an isolated PostgreSQL 16.15 instance.
- The restored database contained 5 public tables and 4 user rows. `vector_generations` was absent in this snapshot.

### ChromaDB

- Stopped the live Chroma service briefly to make a consistent archive of volume `paladin_chromadb_data`, then restarted it.
- Archive: 63,586 bytes; SHA-256 `02149D76303433CD0D2FC17BF219B92E83B4D90C6CD61733AFEA7A1424C94E1C`. It extracted successfully into the scratch volume, and restored collections and data were verified.
- Restored into a separate ChromaDB 0.4.24 service and volume. Restored collections and counts were:
  - `paladin_docs_abac`: 4 chunks
  - `paladin_docs_abac_staging`: 4 chunks
  - `paladin_docs`: 4 chunks
- The four source documents in `paladin_docs_abac` and their classification metadata matched `data/manifest.json`.

### Access matrix

- Loaded the four seeded users from the restored database and document metadata from the restored `paladin_docs_abac` collection.
- Ran the repository policy implementation from retained candidate image `paladin-paladin-api:rc-605d587-lf-20261031-123506` (image ID `f213a976b889`) and queried the restored Chroma collection using each user's actual Chroma metadata filter.
- **16 of 16 user/document cases passed.** Every policy decision matched the expected matrix, and Chroma returned each allowed document while excluding each denied document.

## Live-service and cleanup checks

- All five live Paladin services were healthy after the rehearsal. Live Chroma retained its original three collections with 4 chunks each; service image IDs were unchanged.
- No live PostgreSQL rows or Chroma records were edited. No live ingestion, rollback, generation cutover, or `/ask` request was run.
- The PostgreSQL and Chroma archives, temporary harness, scratch containers, volumes, and network were removed after verification. The temporary backup folder was deleted using ordinary filesystem deletion; this was not a claim of forensic media sanitization.
- During Gate 1 execution, no repository files were changed. This follow-up adds this report and updates the runbook evidence statement; pre-existing working-tree items remain untouched.

## Evidence limits and next gate

This verifies backup creation from the live data sources, restore into disposable services, restored metadata consistency, and the policy-plus-Chroma-filter matrix. It does **not** verify the full HTTP `/ask` route, authentication through the API, LLM generation, generation-pointer resolution, generation-aware ingestion, or rollback against the live stack. It also does not validate restoration into the live services.

**Gate 2 remains NO-GO.** The running live API image and database schema were not upgraded or changed by this rehearsal. Any deployment or cutover requires its own reviewed plan and explicit approval.
