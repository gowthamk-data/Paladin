# Paladin Operations

## Scope and approval

This guide describes a controlled release and recovery process. It is not deployment approval. Before any live change, obtain explicit owner approval for a defined change window, including its start, end, and hard stop. Do not proceed outside the approved scope.

Use immutable image IDs or digests for release decisions. Keep environment-specific gate plans and approval records separate from this sanitized guide.

Each approved window must define a forward-action cutoff early enough to leave time for rollback and verification before its hard stop. For a 90-minute window, the planned cutoff is minute 60, leaving 30 minutes for recovery and closeout. At the cutoff, start no new deployment or ingestion step.

## Pre-window checks

- Confirm that the candidate and rollback images are available; record their immutable IDs.
- Confirm that the candidate was built from reviewed, committed source and that its source-file hashes match the intended commit.
- Check available storage. Prepare a restricted backup location outside the repository and service volumes.
- Use timestamped backup names. Backups contain sensitive database and document data: restrict access, avoid printing contents, and remove temporary transfer and restore copies after verification. Retain the designated backups until the owner explicitly authorizes their deletion.
- Before the window, rehearse the backup-and-restore procedure using trial backups in separate scratch PostgreSQL and Chroma services on a private network, with no host ports and no live volumes. Compare database table/row counts and Chroma collection names, counts, and fingerprints. Remove only those scratch resources after verification.
- Record live service health, PostgreSQL table state, Chroma collection names/counts/fingerprints, and the active pointer.
- Confirm rollback steps and the hard-stop time.

## Backup and verification gate

Before changing the API image or indexed data:

1. Create a custom-format backup of the Paladin database only.
2. Confirm that the archive is non-empty, calculate its SHA-256, and inspect its archive listing.
3. Start the timestamped heartbeat poller below before stopping Chroma. Keep it running in a separate terminal.
4. Stop Chroma only for the approved copy interval, copy its data volume to the restricted backup location, then restart it immediately. Let the poller continue until it records the first HTTP 200 after restart.
5. Record the last HTTP 200 before the stop and the first HTTP 200 after restart; report that observed interval. Do not use Docker's health status as the outage timer because its healthcheck runs less frequently. Abort if the approved outage limit is exceeded.
6. Verify the actual window backups by checksum and archive listing. Restore them into scratch services after the window, or before the owner’s “go” if time allows. Scratch restores must use no host ports or live volumes and must compare database table/row counts and Chroma collection names/counts/fingerprints.
7. Present backup sizes, checksums, archive-listing results, Chroma heartbeat interval, service health, and any completed scratch-restore results to the owner. **Stop and wait for an explicit “go” before deployment or ingestion.** If there is not enough time to restore the actual backups before “go,” schedule that verification after the window; do not cross the forward-action cutoff.

Heartbeat poller (PowerShell 5.1; run in a separate terminal before the Chroma stop). The Python script has no leading indentation so it can be piped to `python -` as written:

```powershell
$heartbeatProbe = @'
import time
import urllib.request
from datetime import datetime

url = "http://chromadb:8000/api/v1/heartbeat"
while True:
    stamp = datetime.now().astimezone().isoformat(timespec="milliseconds")
    try:
        with urllib.request.urlopen(url, timeout=2) as response:
            print(f"{stamp} HTTP {response.status}", flush=True)
    except Exception as exc:
        print(f"{stamp} unavailable ({type(exc).__name__})", flush=True)
    time.sleep(0.5)
'@
$heartbeatProbe | docker compose exec -T paladin-api python -
```

PowerShell 5.1 quoting note: pass SQL to `psql` through standard input rather than embedding SQL inside nested shell quotes. For example:

```powershell
"SELECT name FROM vector_generations WHERE is_active = true;" | docker exec -i <postgres-container> sh -c 'psql -U $POSTGRES_USER -d paladin -t -A'
```

Use the container's `$POSTGRES_USER`; do not assume a username. Set the timestamp and dump-name variable in the same PowerShell session as the `pg_dump` command. For example:

```powershell
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$dumpName = "paladin-$stamp.dump"
docker exec -e DUMP_NAME=$dumpName <postgres-container> sh -c 'pg_dump -U $POSTGRES_USER -d paladin -Fc -f /tmp/$DUMP_NAME'
```

Copy the archive to the restricted backup location, verify its checksum there, then remove only the temporary in-container copy. Keep the retained backup archive.

## API deployment

Deploy only the API service using the approved candidate-image configuration:

```powershell
docker compose -f <base-compose-file> -f <approved-image-override> up -d --no-deps --no-build --pull never paladin-api
```

Never use a bare `docker compose up` for this operation. Verify API health, inspect startup logs, and confirm the running container's image ID equals the approved candidate ID. Do not recreate dependent services.

## Generation-aware ingestion

Run ingestion only after the owner has reviewed the backup evidence and replied “go”:

```powershell
docker compose -f <base-compose-file> -f <approved-image-override> exec -T -e PYTHONPATH=/app paladin-api python data/ingest.py
```

On a fresh generation-aware setup, the ingestion lock protects pointer-table bootstrap. If the legacy collection exists, it is registered as the initial active collection; ingestion builds and validates a new generation, then activates it with a pointer change. No separate manual DDL step is expected.

Ingestion automatically runs a retention pass after activation. On first ingestion from the reviewed baseline, the expected result is that no collection is deleted: the new active generation, its rollback target, and the legacy collection are retained. The normal ingestion command does not print a deletion count. Verify this using the before/after collection inventory, counts, and fingerprints; do not run `--cleanup` as part of the release.

Acceptance checks:

- Exactly one generation pointer is active and points to the new collection.
- The new collection has the expected chunk count and ABAC metadata.
- Protected legacy collections retain their names, counts, and baseline fingerprints.
- The approved authorization matrix passes based on authorization decisions or retrieved sources, not answer text alone.
- Approved smoke requests return expected sources and write audit rows. Do not expose audit row contents or secrets.

## Pointer behavior and rollback

The API resolves the active pointer when it constructs a `VectorStore` for each request. A pointer change therefore affects subsequent requests without an API restart.

Rollback triggers are: API health remains failed beyond the approved recovery threshold; ingestion reports an error; any authorization-matrix or access-result mismatch; audit logging fails; a protected collection fingerprint changes; or the hard stop arrives before acceptance checks pass. Stop forward actions at the approved cutoff and leave the remaining window for rollback and verification.

Before deciding to roll back, query whether `vector_generations` exists and inspect the active pointer. Use the first query before the second; if the first result is empty, the table is absent and there is no pointer to roll back:

```powershell
"SELECT to_regclass('public.vector_generations');" | docker exec -i <postgres-container> sh -c 'psql -U $POSTGRES_USER -d paladin -t -A'
"SELECT name FROM vector_generations WHERE is_active = true;" | docker exec -i <postgres-container> sh -c 'psql -U $POSTGRES_USER -d paladin -t -A'
```

Compare the active collection with the pre-ingestion baseline:

- If the table is absent or activation did not occur and the pointer remains at baseline, **do not** run pointer rollback. Revert the API image only if needed.
- If the new generation is active, run pointer rollback exactly once using the generation-aware image. The rollback command toggles the pointer to the most recently deactivated generation; a second invocation can toggle it forward again.
- If the API is unavailable, use a one-off container from the same immutable generation-aware image, on the application network, with the same required runtime configuration and secrets mounts. Do not substitute another image or environment.

With the approved candidate image configuration, the normal command is:

```powershell
docker compose -f <base-compose-file> -f <approved-image-override> exec -T -e PYTHONPATH=/app paladin-api python data/ingest.py --rollback
```

If the API cannot execute the command, use the same approved image override to create a disposable one-off container instead of restarting the API:

```powershell
docker compose -f <base-compose-file> -f <approved-image-override> run --rm --no-deps --pull never -e PYTHONPATH=/app --entrypoint python paladin-api data/ingest.py --rollback
```

The installed Docker Compose accepted `--pull never`. This one-off command form was syntax-checked with the same flags and candidate image in a disposable Compose project using `-c "print('ok')"`; it returned `ok`. That check used `network_mode: none`, a read-only filesystem, no mounts, and no dependencies. It validates command syntax only, not rollback behavior.

Immediately query the pointer after the single rollback invocation. Confirm it points to the expected prior collection. The pointer table remains, and the generation rows remain with the previous generation active and the rolled-back generation inactive; rollback does not delete collections or rows. If the table state or pointer result differs from expectations, stop and report; do not rerun rollback. Revert the API image only after the pointer is confirmed.

After pointer rollback, rerun the authorization matrix, compare protected-collection fingerprints with baseline, and run both approved `/ask` smoke tests with expected sources and audit records verified. After any API image revert, verify the running container's image ID and API health, then repeat those acceptance checks if the image change could affect request behavior.

A PostgreSQL restore would discard audit rows written since the backup. Treat database restore as a last resort requiring separate explicit authorization. Do not restore PostgreSQL or Chroma over live data as part of routine rollback.

## Fresh deployment

Use this numbered checklist only for a genuinely fresh host; it is not the API-only release procedure above:

1. Prepare `.env` and signing keys following the README. Keep their contents private; do not copy them into this document or commit them.
2. Before making the host reachable, restrict inbound TCP ports 22, 8000, and 8501 at the host/cloud firewall to the owner's current public IP only. The Compose file publishes ports 8000 and 8501 on all interfaces by default, so the firewall rule is essential. TLS termination is a later step; until then, keep access restricted and treat the service as a demo.
3. Set non-default demo-user passwords before making the host reachable. The current seeder has no environment/configuration override and hard-codes demo passwords. Do not expose the host with those defaults; add a supported secret-backed configuration or disable the demo accounts before exposure.
4. Start the fresh stack with `docker compose up -d --build`. This command is for a new host only; it is not the approved command for an existing-stack API release.
5. Pull the configured model with `./scripts/setup_ollama.sh`. On the first ingestion, the embedding model also downloads if it is not already cached (about 270 MB); outbound internet access is required for that download.
6. Run generation-aware ingestion: `docker compose exec -T -e PYTHONPATH=/app paladin-api python data/ingest.py`.
7. Seed missing demo accounts: `docker compose exec -T paladin-api python -m gateway.db.seed`.
8. Run the 16-case authorization/retrieval matrix across four seeded user profiles and four manifest documents. Check each authorization decision or retrieved source; generated answer text alone is not evidence.
9. Run two `/ask` smoke checks: one for an Engineering-scoped source using an Engineering user, and one for a Finance-scoped source using a Finance user. Confirm each expected source and its audit record without exposing credentials or audit-row contents.

On a fresh host with no prior generation, there is no rollback target before the first successful activation. If the first ingestion fails, inspect the pointer and collection state, fix the cause, then rerun ingestion; do not invoke pointer rollback when no prior generation exists. If a legacy collection was adopted, verify that baseline explicitly before retrying.

For a fresh versioned deployment with no pointer table, the first ingestion creates the pointer table and its required index under the ingestion lock. If the configured legacy collection exists, it is adopted before a new generation is built and activated. Verify the active pointer, collection contents, and retention behavior after ingestion. If any prerequisite or state differs from the reviewed baseline, stop rather than guessing at recovery.

## Closeout

Record the window start/end, commit hash, approved image ID, backup sizes and checksums, step timings, pointer state, collection names/counts/fingerprints, health, authorization results, smoke-test source checks, audit confirmation, and deviations. State whether the candidate remains active. Do not include credentials, local filesystem paths, personal usernames, or secret values.
