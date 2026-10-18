"""Generation cutover: activation flip, rollback, retention, failure safety.

Drives the REAL ``DocumentIngestor.ingest_directory()`` flow and the REAL
``GenerationStore`` pointer logic (on SQLite via
``tests.generation_test_support.SQLiteGenerationStore``) against a fake
Chroma client. Each test injects a failure at one stage and asserts the
pointer — what the gateway serves — never moves to a half-built generation,
and that retention keeps exactly {active, rollback target, legacy}.
"""

from __future__ import annotations

import hashlib
import json
import time
import types
from datetime import datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from data.ingest import (
    IN_USE_GRACE_SECONDS,
    DocumentIngestor,
    ManifestError,
    cleanup_generations,
)
from gateway.db.models import Base, GenerationRow
from gateway.rag.generations import GenerationError, GenerationStore
from tests.generation_test_support import (
    FakeChromaClient,
    FakeChromaCollection,
    SQLiteGenerationStore,
)

LEGACY = "paladin_docs_abac"
GEN_1 = f"{LEGACY}_gen_000001"
GEN_2 = f"{LEGACY}_gen_000002"
GEN_3 = f"{LEGACY}_gen_000003"

KNOWN_CONTENT = b"one two three"
KNOWN_SHA256 = hashlib.sha256(KNOWN_CONTENT).hexdigest()


def _manifest():
    return {
        "known.txt": {
            "classification_level": 1,
            "department": "Engineering",
            "visibility": "department",
            "content_sha256": KNOWN_SHA256,
        }
    }


def _make_ingestor(tmp_path, client, store):
    documents = tmp_path / "documents"
    documents.mkdir(exist_ok=True)
    (documents / "known.txt").write_bytes(KNOWN_CONTENT)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(_manifest()), encoding="utf-8")

    ingestor = DocumentIngestor.__new__(DocumentIngestor)
    ingestor.documents_dir = str(documents)
    ingestor.manifest_path = str(manifest_path)
    ingestor.manifest = ingestor._load_manifest()
    ingestor._verified_bytes = {}
    ingestor.client = client
    ingestor.store = store

    class Encoded:
        def tolist(self):
            return [0.1, 0.2]

    ingestor.model = type("Model", (), {"encode": lambda self, text: Encoded()})()
    return ingestor


def _fresh(tmp_path):
    client = FakeChromaClient()
    store = SQLiteGenerationStore(
        create_engine(
            "sqlite://",
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        ),
        legacy_name=LEGACY,
    )
    # Bootstrap the legacy row eagerly and step past the clock tick so every
    # later activation carries a strictly newer activated_at; otherwise
    # previous_name() ordering within one ~15ms tick is ambiguous.
    store.acquire()
    store.release()
    time.sleep(0.03)
    return client, store


class _Pointer:
    """Re-acquired view of the pointer for post-run assertions.

    ingest_directory() releases the store when it finishes, so tests read
    pointer state through this tiny context manager instead.
    """

    def __init__(self, store):
        self.store = store

    def __enter__(self):
        self.store.acquire()
        return self.store

    def __exit__(self, *exc):
        self.store.release()
        return False


def _active(store):
    with _Pointer(store) as acquired:
        return acquired.active_name()


def _previous(store):
    with _Pointer(store) as acquired:
        return acquired.previous_name()


def _candidates(store, in_use_grace_seconds=IN_USE_GRACE_SECONDS):
    with _Pointer(store) as acquired:
        return acquired.cleanup_candidates(
            in_use_grace_seconds=in_use_grace_seconds
        )


def _backdate(store, name, **values):
    """Set explicit timestamps on a generation row (test-only).

    Models a generation whose activation/deactivation happened at a chosen
    time, without sleeping for the real grace period.
    """
    from gateway.db.models import GenerationRow

    store.acquire()
    try:
        conn = store._require_conn()
        conn.execute(
            GenerationRow.__table__.update()
            .where(GenerationRow.__table__.c.name == name)
            .values(**values)
        )
        conn.commit()
    finally:
        store.release()


def _age_past_grace(store, name):
    """Backdate a generation's deactivation beyond the in-use grace window.

    Models a generation deactivated long enough ago that no request can still
    be reading it, without sleeping for the real grace period. The window runs
    from deactivation, so this backdates ``deactivated_at``.
    """
    _backdate(
        store,
        name,
        deactivated_at=datetime.utcnow() - timedelta(seconds=IN_USE_GRACE_SECONDS + 60),
    )


# ---------------------------------------------------------------------------
# Successful cutover: the pointer flips only after build + validation
# ---------------------------------------------------------------------------


def test_first_ingest_bootstraps_legacy_then_cuts_over(tmp_path):
    client, store = _fresh(tmp_path)
    ingestor = _make_ingestor(tmp_path, client, store)

    total = ingestor.ingest_directory()

    assert total > 0
    # The half-built generation is invisible during the build: the only flip
    # the pointer ever saw moved it from the bootstrap legacy row to gen 1.
    assert _active(store) == GEN_1
    assert _previous(store) == LEGACY
    # The built collection holds the chunk, and the legacy bootstrap row is
    # retained as the rollback target (its collection may not exist in Chroma
    # on a fresh deployment; retention is about the pointer row).
    assert client.collections[GEN_1].store
    assert set(_candidates(store)) == set()


def test_second_ingest_cuts_over_and_retains_rollback_target(tmp_path):
    client, store = _fresh(tmp_path)
    _make_ingestor(tmp_path, client, store).ingest_directory()
    _tick()
    _make_ingestor(tmp_path, client, store).ingest_directory()

    assert _active(store) == GEN_2
    # Rollback target: the most recently deactivated generation, still on disk.
    assert _previous(store) == GEN_1
    assert GEN_1 in client.collections
    assert GEN_2 in client.collections
    assert set(_candidates(store)) == set()


def test_cleanup_removes_generations_beyond_retention(tmp_path):
    client, store = _fresh(tmp_path)
    for _ in range(3):
        _make_ingestor(tmp_path, client, store).ingest_directory()
        _tick()

    # Retention set after the third cutover: {gen 3, gen 2 (rollback), legacy}.
    assert _active(store) == GEN_3
    assert _previous(store) == GEN_2
    # gen 1 lost the pointer only moments ago, so the in-use grace keeps it
    # (a request may still be reading it); age it past the grace window and
    # its collection AND pointer row are both removed.
    _age_past_grace(store, GEN_1)

    store.acquire()
    try:
        removed = cleanup_generations(client, store)
    finally:
        store.release()

    assert removed == [GEN_1]
    assert GEN_1 not in client.collections
    assert GEN_1 not in _candidates(store)
    assert set(client.collections) == {GEN_2, GEN_3}


def test_cleanup_holds_recently_deactivated_generation_for_in_flight_requests(
    tmp_path,
):
    """A generation deactivated seconds ago is outside the retention set but
    may still be serving a request that resolved the pointer just before the
    cutover; retention must hold it for the in-use grace window."""
    client, store = _fresh(tmp_path)
    for _ in range(3):
        _make_ingestor(tmp_path, client, store).ingest_directory()
        _tick()

    assert _active(store) == GEN_3
    # gen 1 is not active and not the rollback target, yet it survives.
    store.acquire()
    try:
        assert cleanup_generations(client, store) == []
    finally:
        store.release()

    assert GEN_1 in client.collections
    assert GEN_1 in _candidates(store, in_use_grace_seconds=0.0)

    # Once the grace window has passed the same run reclaims it.
    _age_past_grace(store, GEN_1)
    store.acquire()
    try:
        assert cleanup_generations(client, store) == [GEN_1]
    finally:
        store.release()
    assert GEN_1 not in client.collections


def test_database_index_prevents_two_active_rows(tmp_path):
    """The one-active invariant is enforced by the database, not only by
    activate()'s transaction: a second active row — whatever the calling code
    does — is rejected at insert time, and the failed insert leaves the
    single-active invariant intact.
    """
    client, store = _fresh(tmp_path)
    store.acquire()
    try:
        conn = store._require_conn()
        with conn.begin():
            active_count = conn.execute(
                sa.select(sa.func.count())
                .select_from(GenerationRow)
                .where(GenerationRow.is_active.is_(True))
            ).scalar()
        assert active_count == 1
        with pytest.raises(sa.exc.IntegrityError):
            with conn.begin():
                conn.execute(
                    sa.insert(GenerationRow).values(
                        name=GEN_2,
                        is_active=True,
                        activated_at=datetime.utcnow(),
                    )
                )
        with conn.begin():
            active_count = conn.execute(
                sa.select(sa.func.count())
                .select_from(GenerationRow)
                .where(GenerationRow.is_active.is_(True))
            ).scalar()
        assert active_count == 1
    finally:
        store.release()


def test_existing_pointer_table_gains_deactivated_at(tmp_path):
    """A pointer table from an earlier deployment (no deactivated_at column)
    is migrated in place: acquire() adds the column instead of failing or
    leaving cleanup without the field its grace window now uses.
    """
    client = FakeChromaClient()
    engine = create_engine(
        "sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    # Pre-create the ORIGINAL schema exactly as the first deployment had it.
    conn = engine.connect()
    with conn.begin():
        conn.execute(
            sa.text(
                "CREATE TABLE vector_generations ("
                " name VARCHAR(255) PRIMARY KEY,"
                " is_active BOOLEAN NOT NULL DEFAULT false,"
                " activated_at TIMESTAMP,"
                " created_at TIMESTAMP NOT NULL)"
            )
        )
    conn.close()

    store = SQLiteGenerationStore(engine, legacy_name=LEGACY)
    store.collection_exists = lambda name: name in client.collections
    store.acquire()
    try:
        columns = {
            column["name"]
            for column in sa.inspect(engine).get_columns("vector_generations")
        }
        assert "deactivated_at" in columns
        # The migrated store still enforces the one-active invariant.
        conn = store._require_conn()
        with conn.begin():
            conn.execute(
                sa.insert(GenerationRow).values(
                    name=GEN_1, is_active=True, activated_at=datetime.utcnow()
                )
            )
        with pytest.raises(sa.exc.IntegrityError):
            with conn.begin():
                conn.execute(
                    sa.insert(GenerationRow).values(
                        name=GEN_2, is_active=True, activated_at=datetime.utcnow()
                    )
                )
    finally:
        store.release()


def test_activation_records_deactivation_of_the_previous_generation(tmp_path):
    """Cutover stamps the superseded generation with deactivated_at — the
    timestamp the in-use grace window is measured from."""
    client, store = _fresh(tmp_path)
    _make_ingestor(tmp_path, client, store).ingest_directory()
    _tick()
    _make_ingestor(tmp_path, client, store).ingest_directory()
    assert _active(store) == GEN_2
    assert _previous(store) == GEN_1

    with _Pointer(store) as acquired:
        conn = acquired._require_conn()
        rows = conn.execute(
            sa.select(
                GenerationRow.name,
                GenerationRow.is_active,
                GenerationRow.activated_at,
                GenerationRow.deactivated_at,
            )
        ).all()
    state = {
        name: (bool(is_active), activated_at, deactivated_at)
        for name, is_active, activated_at, deactivated_at in rows
    }
    # The active generation was activated and never deactivated.
    assert state[GEN_2][0] is True
    assert state[GEN_2][1] is not None
    assert state[GEN_2][2] is None
    # The superseded generation kept its activation (rollback ordering) and
    # gained a deactivation timestamp at cutover.
    assert state[GEN_1][0] is False
    assert state[GEN_1][1] is not None
    assert state[GEN_1][2] is not None


def test_grace_window_runs_from_deactivation_not_activation(tmp_path):
    """A long-serving generation keeps its full in-use grace after cutover:
    the window is measured from deactivation, so a generation that served for
    weeks is not reclaimable the moment it loses the pointer.
    """
    client, store = _fresh(tmp_path)
    _make_ingestor(tmp_path, client, store).ingest_directory()
    _tick()
    _make_ingestor(tmp_path, client, store).ingest_directory()
    assert _active(store) == GEN_2
    assert _previous(store) == GEN_1

    # Backdate ACTIVATION far into the past (it "served" for weeks) but leave
    # deactivation recent: the grace window must run from deactivated_at.
    _backdate(store, GEN_1, activated_at=datetime.utcnow() - timedelta(days=30))
    store.acquire()
    try:
        assert cleanup_generations(client, store) == []
    finally:
        store.release()
    assert GEN_1 in client.collections

    # Age the DEACTIVATION past the grace window: now it is reclaimable.
    _backdate(
        store,
        GEN_1,
        deactivated_at=datetime.utcnow() - timedelta(seconds=IN_USE_GRACE_SECONDS + 60),
    )
    store.acquire()
    try:
        assert cleanup_generations(client, store) == [GEN_1]
    finally:
        store.release()
    assert GEN_1 not in client.collections


def test_fresh_install_without_legacy_collection_starts_clean(tmp_path):
    """No legacy collection in Chroma and no pointer rows: nothing is
    registered as a rollback target, so rollback refuses rather than flipping
    to a collection that never existed."""
    client = FakeChromaClient()
    store = SQLiteGenerationStore(
        create_engine(
            "sqlite://",
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        ),
        legacy_name=LEGACY,
    )
    store.collection_exists = lambda name: name in client.collections
    assert LEGACY not in client.collections

    store.acquire()
    try:
        # Bootstrap adopted nothing: no pointer row exists at all.
        assert store.active_name() is None
        assert store.previous_name() is None
        # Gen 1 is the first name, not gen 2, because no legacy row consumed
        # a slot or was ever registered.
        assert store.next_generation_name() == GEN_1
        with pytest.raises(GenerationError, match="no previous generation"):
            store.rollback()
    finally:
        store.release()

    # The first real ingestion activates gen 1 directly, with no legacy row.
    _make_ingestor(tmp_path, client, store).ingest_directory()
    assert _active(store) == GEN_1
    assert _previous(store) is None
    assert LEGACY not in client.collections


def test_rollback_refuses_a_target_whose_collection_is_gone(tmp_path):
    """A registered rollback target whose Chroma collection is missing must
    not be flipped to: that would take serving from degraded to dead."""
    client, store = _fresh(tmp_path)
    _make_ingestor(tmp_path, client, store).ingest_directory()
    _tick()
    _make_ingestor(tmp_path, client, store).ingest_directory()
    assert _previous(store) == GEN_1

    # The rollback target's collection disappears (volume loss, manual delete).
    del client.collections[GEN_1]
    store.collection_exists = lambda name: name in client.collections

    store.acquire()
    try:
        with pytest.raises(GenerationError, match="collection is missing"):
            store.rollback()
        # Pointer never moved: gen 2 still serves.
        assert store.active_name() == GEN_2
        # The row survives so a restored collection can still be rolled back to.
        assert store.previous_name() == GEN_1
    finally:
        store.release()

    # Restore the collection and the same rollback succeeds.
    client.collections[GEN_1] = FakeChromaCollection()
    store.acquire()
    try:
        assert store.rollback() == GEN_1
    finally:
        store.release()
    assert _active(store) == GEN_1


def test_rollback_restores_previous_generation(tmp_path):
    client, store = _fresh(tmp_path)
    _make_ingestor(tmp_path, client, store).ingest_directory()
    _tick()
    _make_ingestor(tmp_path, client, store).ingest_directory()

    store.acquire()
    try:
        assert store.rollback() == GEN_1
    finally:
        store.release()

    assert _active(store) == GEN_1
    assert _previous(store) == GEN_2
    # Both collections survived: rollback is a pointer flip, not a copy.
    assert GEN_1 in client.collections and GEN_2 in client.collections


def _seed_legacy_collection(client):
    """Give the fake Chroma a legacy collection, as a real deployment has."""
    collection = FakeChromaCollection()
    collection.store["old::chunk-0"] = {
        "ids": "old::chunk-0",
        "documents": "previous good content",
        "embeddings": [0.9, 0.9],
        "metadatas": {"source_document": "old.txt"},
    }
    client.collections[LEGACY] = collection


def test_rollback_without_previous_generation_refuses(tmp_path):
    _, store = _fresh(tmp_path)
    store.acquire()
    try:
        with pytest.raises(GenerationError, match="no previous generation"):
            store.rollback()
    finally:
        store.release()


def test_processing_failure_keeps_pointer_on_legacy(tmp_path):
    client, store = _fresh(tmp_path)
    _seed_legacy_collection(client)
    ingestor = _make_ingestor(tmp_path, client, store)

    def flaky_process(collection, filename, data):
        raise RuntimeError("boom-process")

    ingestor._process_file = flaky_process
    with pytest.raises(RuntimeError, match="boom-process"):
        ingestor.ingest_directory()

    # Pointer untouched: still the legacy bootstrap row.
    assert _active(store) == LEGACY
    # The half-built generation was discarded: collection gone, row forgotten.
    assert GEN_1 not in client.collections
    assert GEN_1 not in _candidates(store)
    # The legacy collection's content is intact.
    assert client.collections[LEGACY].store["old::chunk-0"]["documents"] == (
        "previous good content"
    )
    # The advisory lock was released despite the crash: a fresh acquire works.
    store.acquire()
    store.release()


def test_validation_failure_keeps_pointer_on_legacy(tmp_path):
    client, store = _fresh(tmp_path)
    _seed_legacy_collection(client)
    ingestor = _make_ingestor(tmp_path, client, store)

    real_validate = ingestor._validate_collection

    def bad_validate(collection, expected_chunks):
        # Inject an unmanifested row so real validation must reject it.
        collection.upsert(
            ids=["rogue::chunk-9"],
            embeddings=[[0.0, 0.0]],
            documents=["rogue"],
            metadatas=[
                {
                    "source_document": "unknown.txt",
                    "chunk_index": 9,
                    "classification_level": 1,
                    "department": "Engineering",
                    "visibility": "department",
                }
            ],
        )
        return real_validate(collection, expected_chunks + 1)

    ingestor._validate_collection = bad_validate
    with pytest.raises(ManifestError, match="unmanifested"):
        ingestor.ingest_directory()

    assert _active(store) == LEGACY
    assert GEN_1 not in client.collections
    assert GEN_1 not in _candidates(store)


def test_empty_generation_is_refused_before_activation(tmp_path):
    client, store = _fresh(tmp_path)
    _seed_legacy_collection(client)
    ingestor = _make_ingestor(tmp_path, client, store)

    # Every file yields zero chunks (e.g. a pathological chunker regression).
    ingestor._process_file = lambda collection, filename, data: 0
    with pytest.raises(ManifestError, match="empty generation"):
        ingestor.ingest_directory()

    assert _active(store) == LEGACY
    assert GEN_1 not in client.collections


def test_activation_of_unregistered_generation_refuses(tmp_path):
    _, store = _fresh(tmp_path)
    store.acquire()
    try:
        with pytest.raises(GenerationError, match="not registered"):
            store.activate(f"{LEGACY}_gen_999999")
        # The failed activation left the pointer on the bootstrap row.
        assert store.active_name() == LEGACY
    finally:
        store.release()


def test_forget_refuses_the_active_generation(tmp_path):
    _, store = _fresh(tmp_path)
    store.acquire()
    try:
        with pytest.raises(GenerationError, match="refusing to forget"):
            store.forget(LEGACY)
        assert store.active_name() == LEGACY
    finally:
        store.release()


# ---------------------------------------------------------------------------
# cleanup_generations: retention mechanics against a fake Chroma
# ---------------------------------------------------------------------------


def _plant_orphan(client, store, name):
    """Simulate a crashed run's leftover: a registered row + collection that
    never became active (so it is outside the retention set)."""
    from datetime import datetime

    from gateway.db.models import GenerationRow

    store.register(name)
    client.collections[name] = FakeChromaCollection()
    # Give it a real (past) activation history so it looks like an old
    # generation rather than a never-used registration.
    store._require_conn().execute(
        GenerationRow.__table__.update()
        .where(GenerationRow.__table__.c.name == name)
        .values(activated_at=datetime(2020, 1, 1))
    )
    store._require_conn().commit()


def _tick():
    # datetime.utcnow() resolution on Windows is ~15ms; two activations
    # within one tick would tie and make previous_name() ordering ambiguous.
    # Real ingestions are minutes apart; tests compress that with a sleep.
    time.sleep(0.03)


def test_cleanup_deletes_orphans_but_keeps_retention_set(tmp_path):
    """Active + rollback target + legacy survive; orphans are removed."""
    client, store = _fresh(tmp_path)
    _make_ingestor(tmp_path, client, store).ingest_directory()
    _tick()
    _make_ingestor(tmp_path, client, store).ingest_directory()

    # A crashed run left this behind; it is outside {active, previous, legacy}.
    store.acquire()
    try:
        _plant_orphan(client, store, GEN_3)
    finally:
        store.release()

    store.acquire()
    try:
        removed = cleanup_generations(client, store)
    finally:
        store.release()

    assert removed == [GEN_3]
    assert GEN_3 not in client.collections
    # Retention set survives.
    assert set(client.collections) == {GEN_1, GEN_2}


def test_cleanup_drops_rows_whose_collection_is_already_gone(tmp_path):
    """A dangling row (collection deleted by a crashed run) must not wedge
    retention forever: the row is dropped so the next run retries cleanly."""
    client, store = _fresh(tmp_path)
    _make_ingestor(tmp_path, client, store).ingest_directory()
    _tick()
    _make_ingestor(tmp_path, client, store).ingest_directory()

    # Simulate a prior partial cleanup: an orphan row survived but its
    # collection was already deleted.
    store.acquire()
    try:
        store.register(GEN_3)
    finally:
        store.release()

    store.acquire()
    try:
        removed = cleanup_generations(client, store)
    finally:
        store.release()

    assert removed == [GEN_3]
    assert set(client.collections) == {GEN_1, GEN_2}


def test_cleanup_keeps_row_when_collection_delete_fails(tmp_path):
    """A transient Chroma failure keeps the row so the next run retries;
    cleanup must not raise — it is best-effort and post-cutover."""
    client, store = _fresh(tmp_path)
    _make_ingestor(tmp_path, client, store).ingest_directory()
    _tick()
    _make_ingestor(tmp_path, client, store).ingest_directory()

    store.acquire()
    try:
        _plant_orphan(client, store, GEN_3)
    finally:
        store.release()

    original_delete = client.delete_collection

    def flaky_delete(name):
        if name == GEN_3:
            raise ConnectionError("chroma briefly unreachable")
        return original_delete(name)

    client.delete_collection = flaky_delete
    store.acquire()
    try:
        removed = cleanup_generations(client, store)
    finally:
        store.release()

    # No raise, nothing removed, and the orphan row survives for the next run.
    assert removed == []
    assert GEN_3 in client.collections
    assert GEN_3 in _candidates(store)


# ---------------------------------------------------------------------------
# Chroma error classification: absent vs unreachable (shapes verified against
# a disposable chromadb 0.4.24 instance)
# ---------------------------------------------------------------------------


def test_pinned_chroma_missing_collection_is_a_plain_exception():
    """Pin the real 0.4.24 driver shape: 'absent' is NOT a ValueError.

    Verified against a disposable ``chromadb/chroma:0.4.24`` instance:
    ``get_collection`` on a missing name raises ``Exception`` whose message is
    the serialised server error. A probe that only caught ``ValueError`` would
    therefore crash on every fresh install instead of bootstrapping.
    """
    from data.ingest import _is_missing_collection

    real = Exception('{"error":"ValueError(\'Collection paladin_docs_abac does not exist.\')"}')
    assert type(real) is Exception
    assert _is_missing_collection(real, "paladin_docs_abac") is True
    # Name mismatch: an error about another collection is not evidence here.
    assert _is_missing_collection(real, "paladin_docs_abac_gen_000001") is False


def test_pinned_chroma_unreachable_server_is_a_valueerror_and_fails_closed():
    """'Unreachable' is the ValueError shape, and must never read as absent."""
    from data.ingest import _is_missing_collection

    unreachable = ValueError(
        "Could not connect to a Chroma server. Are you sure it is running?"
    )
    assert _is_missing_collection(unreachable, "paladin_docs_abac") is False


def test_probe_treats_missing_collection_as_absent():
    client = FakeChromaClient()
    from data.ingest import _probe_collection_exists

    assert _probe_collection_exists(client, "nope") is False


def test_probe_propagates_unreachable_chroma_instead_of_reporting_absent(tmp_path):
    """A half-dead Chroma must fail closed, never look like a fresh install:
    reporting 'absent' would make bootstrap adopt a nonexistent collection."""
    client = FakeChromaClient()
    client.raise_unreachable()

    from data.ingest import _probe_collection_exists

    with pytest.raises(ValueError, match="Could not connect"):
        _probe_collection_exists(client, LEGACY)


def test_unreachable_chroma_refuses_ingestion_rather_than_bootstrapping(tmp_path):
    """End-to-end: the ingestor must raise, not silently start generation 1
    against a Chroma it cannot reach."""
    from data.ingest import _probe_collection_exists

    client = FakeChromaClient()
    store = SQLiteGenerationStore(
        create_engine(
            "sqlite://",
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        ),
        legacy_name=LEGACY,
    )
    # Bootstrap/rollback probes must consult the (dead) client. The store is
    # deliberately NOT pre-acquired here, so ingest_directory()'s own acquire()
    # is what runs the probe and must fail.
    store.collection_exists = lambda name: _probe_collection_exists(client, name)
    client.raise_unreachable()

    ingestor = _make_ingestor(tmp_path, client, store)
    with pytest.raises(ValueError, match="Could not connect"):
        ingestor.ingest_directory()

    # Nothing was adopted or activated, and the lock was released. Read the
    # pointer directly: the store's own probe is still wired to the dead
    # client, so going through acquire() here would fail for the wrong reason.
    from gateway.db.models import GenerationRow

    with store.engine.connect() as conn:
        rows = conn.execute(sa.select(GenerationRow.name)).scalars().all()
    assert rows == []
    assert store.next_generation_name() == GEN_1
    store.release()


def test_cleanup_keeps_row_when_chroma_is_unreachable(tmp_path):
    """An unreachable Chroma is not proof a collection is gone, so the row
    must survive for the next run (otherwise retention forgets a collection
    that may still hold the only copy)."""
    client, store = _fresh(tmp_path)
    _make_ingestor(tmp_path, client, store).ingest_directory()
    _tick()
    _make_ingestor(tmp_path, client, store).ingest_directory()

    store.acquire()
    try:
        _plant_orphan(client, store, GEN_3)
    finally:
        store.release()

    original_delete = client.delete_collection

    def dead_delete(name):
        raise ValueError("Could not connect to a Chroma server. Are you sure it is running?")

    client.delete_collection = dead_delete
    store.acquire()
    try:
        removed = cleanup_generations(client, store)
    finally:
        store.release()

    assert removed == []
    assert GEN_3 in _candidates(store)
    client.delete_collection = original_delete
    assert GEN_3 in client.collections


def test_lock_refuses_concurrent_ingestion(tmp_path):
    """The advisory lock serialises ingestions: a second acquire fails fast."""
    _, store = _fresh(tmp_path)
    store.acquire()
    try:
        other = SQLiteGenerationStore(store.engine, legacy_name=LEGACY)
        # SQLite has no advisory lock, so model it: the real acquire() raises
        # GenerationError when pg_try_advisory_lock returns false. Mirror the
        # contract here by asserting the double-acquire guard on one store.
        with pytest.raises(GenerationError, match="already acquired"):
            store.acquire()
        assert other._conn is None
    finally:
        store.release()


# ---------------------------------------------------------------------------
# acquire()'s own failure path (PostgreSQL-only, so driven with doubles that
# still execute the REAL GenerationStore.acquire/release code)
# ---------------------------------------------------------------------------


class _FakeResult:
    def __init__(self, scalar):
        self._scalar = scalar

    def scalar(self):
        return self._scalar

    def first(self):
        # No rows: the fake carries no pg_indexes entry, i.e. no pre-existing
        # index of the older shape for _ensure_one_active_index to replace.
        return None


class _FakeAdvisoryConn:
    """Connection double that records the advisory lock/unlock sequence.

    ``acquire()`` runs the column migrations, ``CREATE UNIQUE INDEX``,
    ``pg_try_advisory_lock`` and then ``bootstrap_legacy``; ``release()`` runs
    ``pg_advisory_unlock`` and closes. The double makes both observable.
    """

    def __init__(self, try_lock_result=True):
        self.executed: list[str] = []
        self.try_lock_result = try_lock_result
        self.closed = False
        self.commits = 0

    def execute(self, statement, params=None):
        text = str(statement)
        self.executed.append(text)
        if "pg_try_advisory_lock" in text:
            return _FakeResult(self.try_lock_result)
        if "pg_advisory_unlock" in text:
            self.executed.append("<<unlock>>")
            return _FakeResult(True)
        if "pg_indexes" in text:
            # _ensure_one_active_index looks for an older-shape index to drop;
            # the fake has none, so it reports no row.
            return _FakeResult(None)
        return None

    def begin(self):
        return _FakeTransaction()

    def commit(self):
        self.commits += 1

    def close(self):
        self.closed = True


class _FakeTransaction:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakePgEngine:
    """Engine double whose dialect claims PostgreSQL so the real acquire()'s
    guard passes, and whose create_all is a no-op (the pointer table is faked
    by the underlying SQLite-less connection)."""

    dialect = types.SimpleNamespace(name="postgresql")

    def __init__(self, conn):
        self._conn = conn

    def connect(self):
        return self._conn


def _real_store_with_failing_bootstrap(monkeypatch, conn):
    """A GenerationStore running the REAL acquire()/release() against doubles.

    The engine claims the PostgreSQL dialect so acquire()'s guard passes;
    ``create_all`` is stubbed because the fake connection has no DDL, and
    ``bootstrap_legacy`` is replaced by a failing version so the acquire()
    failure path is exercised.
    """
    from gateway.db import models
    from gateway.rag import generations

    monkeypatch.setattr(
        models.Base.metadata,
        "create_all",
        lambda **kwargs: None,
        raising=False,
    )
    store = generations.GenerationStore(engine=_FakePgEngine(conn))

    def failing_bootstrap(self, legacy_name):
        raise GenerationError("pointer state corrupt: rows exist but none is active")

    monkeypatch.setattr(generations.GenerationStore, "bootstrap_legacy", failing_bootstrap)
    return store


def test_acquire_releases_lock_and_closes_conn_when_bootstrap_fails(monkeypatch):
    """A failing bootstrap must not leak the advisory lock or the connection:
    leaving either behind makes every later ingestion refuse to run."""
    from gateway.rag.generations import GenerationStore

    conn = _FakeAdvisoryConn()
    store = _real_store_with_failing_bootstrap(monkeypatch, conn)

    with pytest.raises(GenerationError, match="none is active"):
        store.acquire()

    # The connection was closed and the advisory lock explicitly released...
    assert conn.closed is True
    assert "<<unlock>>" in conn.executed
    # ...and the store is left un-acquired, so a retry is possible.
    assert store._conn is None


def test_acquire_refuses_concurrent_lock_without_holding_it(monkeypatch):
    """``pg_try_advisory_lock`` returning false must close the connection
    WITHOUT unlocking — the lock belongs to the other session."""
    from gateway.rag.generations import GenerationStore

    conn = _FakeAdvisoryConn(try_lock_result=False)
    store = GenerationStore(engine=_FakePgEngine(conn))
    monkeypatch.setattr(Base.metadata, "create_all", lambda **kwargs: None, raising=False)

    def noop_bootstrap(self, legacy_name):
        raise AssertionError("bootstrap must not run when the lock is refused")

    monkeypatch.setattr(GenerationStore, "bootstrap_legacy", noop_bootstrap)
    with pytest.raises(GenerationError, match="another ingestion holds"):
        store.acquire()

    assert conn.closed is True
    assert "<<unlock>>" not in conn.executed
    assert store._conn is None
    assert conn.closed is True
    assert "<<unlock>>" not in conn.executed
    assert store._conn is None


# ---------------------------------------------------------------------------
# release()'s own failure path (again driven with doubles that execute the
# REAL GenerationStore.release() code). An unlock or commit failure must
# invalidate the connection rather than hand a still-lock-holding session
# back to the pool, and must re-raise the ORIGINAL failure.
# ---------------------------------------------------------------------------


class _UnlockFailure(RuntimeError):
    """Simulates pg_advisory_unlock failing server-side or in transit."""


class _CommitFailure(RuntimeError):
    """Simulates the commit that follows pg_advisory_unlock failing."""


class _FailingReleaseConn(_FakeAdvisoryConn):
    """Connection double whose pg_advisory_unlock and/or commit raise.

    Both mirror real failure modes: with an unlock error the lock may never
    have reached the server, and with a commit error the unlock ran but the
    transaction never closed cleanly. In either case a plain close() would
    check the connection back into the pool STILL HOLDING the session-level
    advisory lock, so the real release() must invalidate() it instead.
    """

    def __init__(self, *, unlock_fails=False, commit_fails=False):
        super().__init__()
        self._unlock_fails = unlock_fails
        self._commit_fails = commit_fails
        self.invalidated = False
        self.close_calls = 0

    def execute(self, statement, params=None):
        text = str(statement)
        if "pg_advisory_unlock" in text and self._unlock_fails:
            self.executed.append(text)
            raise _UnlockFailure("server closed the connection unexpectedly")
        return super().execute(statement, params)

    def commit(self):
        if self._commit_fails:
            raise _CommitFailure("could not commit: connection lost mid-transaction")
        super().commit()

    def invalidate(self):
        self.invalidated = True

    def close(self):
        self.close_calls += 1
        super().close()


@pytest.mark.parametrize(
    "conn_kwargs, expected_type",
    [
        ({"unlock_fails": True}, _UnlockFailure),
        ({"commit_fails": True}, _CommitFailure),
    ],
    ids=["unlock-fails", "commit-fails"],
)
def test_release_invalidates_conn_and_clears_store_when_unlock_or_commit_fails(
    conn_kwargs, expected_type
):
    """release() must never return a still-lock-holding connection to the pool.

    If pg_advisory_unlock or the follow-up commit fails, the session-level
    advisory lock may still be owned by that connection. close() alone would
    check it back in holding the lock, and every later ingestion would then be
    refused forever — so release() must invalidate() it. It must ALSO leave
    the store un-acquired and re-raise the ORIGINAL failure rather than
    letting a cleanup error mask it.
    """
    from gateway.rag.generations import GenerationStore

    conn = _FailingReleaseConn(**conn_kwargs)
    store = GenerationStore(engine=_FakePgEngine(conn))
    store._conn = conn  # as acquire() would leave it; release() must clear it

    with pytest.raises(expected_type) as raised:
        store.release()

    # The original unlock/commit failure is what propagates, unchanged.
    assert type(raised.value) is expected_type
    # The connection was INVALIDATED (not merely closed), so it can never be
    # handed to the next pool borrower holding the session lock — and it was
    # closed as well.
    assert conn.invalidated is True
    assert conn.close_calls >= 1
    # The store does not remain acquired, so a later acquire() can be retried.
    assert store._conn is None


def test_release_still_plainly_closes_on_success():
    """Happy path is unchanged: unlock, commit, close — and no invalidation."""
    from gateway.rag.generations import GenerationStore

    conn = _FakeAdvisoryConn()
    store = GenerationStore(engine=_FakePgEngine(conn))
    store._conn = conn

    store.release()

    assert "<<unlock>>" in conn.executed
    assert conn.commits == 1
    assert conn.closed is True
    assert store._conn is None

