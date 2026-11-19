"""Pointer resolution and fail-closed behaviour of the generation pointer.

These tests cover the READ side of the cutover design:

* ``read_active_generation`` resolves the one active row, tolerates the
  pre-versioning bootstrap cases (missing table / empty table -> ``None``),
  without poisoning the caller's transaction,
  meaning "serve the legacy collection", and propagates every other database
  failure so the gateway can fail closed.
* ``VectorStore`` construction resolves the pointer per request on the passed
  DB session (positional-first: the route calls ``VectorStore(db)`` and an
  earlier ``collection_name``-first signature silently swallowed the session),
  falls back to the legacy collection name only when the pointer is absent,
  and turns pointer/collection failures into ``ServiceUnavailableError``.

The GenerationStore MUTATION side (register/activate/rollback/cleanup) is
covered by ``test_generation_cutover.py``.
"""

from __future__ import annotations

import json
import subprocess
import sys
import types
import uuid
from datetime import datetime

import pytest
import sqlalchemy as sa
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gateway.db.models import Base, GenerationRow
from gateway.errors import ServiceUnavailableError
from gateway.rag.generations import (
    GenerationError,
    read_active_generation,
)
from gateway.rag.vector_store import VectorStore

GEN_1 = "paladin_docs_abac_gen_000001"
GEN_2 = "paladin_docs_abac_gen_000002"


def _session(engine):
    Base.metadata.create_all(bind=engine, tables=[GenerationRow.__table__])
    return sessionmaker(bind=engine)()


def _pointer_engine():
    # StaticPool: every session must see the same in-memory database.
    return create_engine(
        "sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )


def _insert_pointer(session, name, is_active, activated_at=None):
    session.add(
        GenerationRow(name=name, is_active=is_active, activated_at=activated_at)
    )
    session.commit()


# ---------------------------------------------------------------------------
# read_active_generation
# ---------------------------------------------------------------------------


def test_missing_pointer_table_resolves_to_none():
    # No create_all: catalog inspection must resolve the bootstrap case without
    # issuing a failing query against the absent table.
    engine = create_engine("sqlite://")
    session = sessionmaker(bind=engine)()
    assert read_active_generation(session) is None
    assert session.execute(sa.text("SELECT 1")).scalar_one() == 1


def test_empty_pointer_table_resolves_to_none():
    session = _session(_pointer_engine())
    assert read_active_generation(session) is None


def test_rows_without_active_row_fail_closed():
    # A table with rows but no active row is corrupt (crashed flip, manual
    # edit): resolution must raise GenerationError so the route fails closed
    # as 503, never silently serve the stale legacy collection.
    session = _session(_pointer_engine())
    _insert_pointer(session, "paladin_docs_abac", False, datetime(2026, 1, 1))
    _insert_pointer(session, GEN_1, False, datetime(2026, 1, 2))
    with pytest.raises(GenerationError, match="none is active"):
        read_active_generation(session)


def test_active_generation_is_resolved():
    session = _session(_pointer_engine())
    _insert_pointer(session, "paladin_docs_abac", False, datetime(2026, 1, 1))
    _insert_pointer(session, GEN_1, True, datetime(2026, 1, 2))
    assert read_active_generation(session) == GEN_1


def test_multiple_active_rows_fail_closed():
    # Two active rows is corrupt state, not a newest-wins race. The one-active
    # index makes it impossible in a healthy deployment, but a manually edited
    # or partially migrated table must never silently resolve to one of the
    # rows — resolution raises so the gateway fails closed as 503.
    session = _session(_pointer_engine())
    _insert_pointer(session, GEN_1, True, datetime(2026, 1, 1))
    _insert_pointer(session, GEN_2, True, datetime(2026, 1, 2))
    with pytest.raises(GenerationError, match="multiple active"):
        read_active_generation(session)


def test_single_active_row_still_resolves():
    session = _session(_pointer_engine())
    _insert_pointer(session, "paladin_docs_abac", False, datetime(2026, 1, 1))
    _insert_pointer(session, GEN_1, True, datetime(2026, 1, 2))
    assert read_active_generation(session) == GEN_1


def test_one_active_index_ddl_keys_on_the_flag():
    # The constraint must key on is_active — unique among active rows — so two
    # differently-named active rows are impossible. The original DDL keyed on
    # name, which only forbade a duplicated active name and let two active
    # rows coexist.
    from gateway.rag.generations import ONE_ACTIVE_INDEX_DDL

    assert "ON vector_generations (is_active) WHERE is_active" in ONE_ACTIVE_INDEX_DDL
    assert "(name) WHERE" not in ONE_ACTIVE_INDEX_DDL


def test_non_missing_database_failure_propagates():
    class DeadSession:
        def connection(self):
            raise SQLAlchemyError("connection refused")

    with pytest.raises(SQLAlchemyError, match="connection refused"):
        read_active_generation(DeadSession())


# ---------------------------------------------------------------------------
# VectorStore pointer resolution (chromadb is faked; no live Chroma)
# ---------------------------------------------------------------------------


class _FakeGetCollection:
    def __init__(self):
        self.requested = []

    def __call__(self, name, **kwargs):
        self.requested.append(name)
        return types.SimpleNamespace(name=name)


@pytest.fixture
def fake_chromadb(monkeypatch):
    get_collection = _FakeGetCollection()
    client = types.SimpleNamespace(get_collection=get_collection)
    module = types.SimpleNamespace(HttpClient=lambda **kwargs: client)
    monkeypatch.setitem(sys.modules, "chromadb", module)
    return get_collection


def test_vector_store_serves_the_active_generation(fake_chromadb, monkeypatch):
    monkeypatch.setattr(
        "gateway.rag.vector_store.read_active_generation", lambda db=None: GEN_2
    )
    store = VectorStore(object())
    assert store.generation == GEN_2
    assert fake_chromadb.requested == [GEN_2]


def test_vector_store_passes_route_session_to_pointer(fake_chromadb, monkeypatch):
    # Route-level contract: query_routes.py calls ``VectorStore(db)`` with the
    # request session positionally. A constructor whose first parameter were
    # ``collection_name`` would bind the Session as the collection name and
    # silently serve the wrong collection (or fail closed on a bogus name).
    seen = {}

    def spy(db=None):
        seen["db"] = db
        return None

    monkeypatch.setattr("gateway.rag.vector_store.read_active_generation", spy)
    session = object()
    store = VectorStore(session)
    assert seen["db"] is session
    # Legacy fallback uses the configured collection NAME, not the session.
    from gateway.config import CHROMA_COLLECTION

    assert isinstance(store.generation, str)
    assert store.generation == CHROMA_COLLECTION
    assert fake_chromadb.requested == [CHROMA_COLLECTION]


def test_vector_store_falls_back_to_legacy_without_pointer(fake_chromadb, monkeypatch):
    monkeypatch.setattr(
        "gateway.rag.vector_store.read_active_generation", lambda db=None: None
    )
    from gateway.config import CHROMA_COLLECTION

    store = VectorStore(object())
    assert store.generation == CHROMA_COLLECTION
    assert fake_chromadb.requested == [CHROMA_COLLECTION]


def test_vector_store_fails_closed_on_pointer_failure(fake_chromadb, monkeypatch):
    def boom(db=None):
        raise OperationalError(
            "SELECT", {}, Exception("could not connect to server")
        )

    monkeypatch.setattr("gateway.rag.vector_store.read_active_generation", boom)
    with pytest.raises(ServiceUnavailableError):
        VectorStore(object())
    assert fake_chromadb.requested == []


def test_vector_store_fails_closed_on_corrupt_pointer(fake_chromadb, monkeypatch):
    def corrupt(db=None):
        raise GenerationError("pointer state corrupt: rows exist but none is active")

    monkeypatch.setattr("gateway.rag.vector_store.read_active_generation", corrupt)
    with pytest.raises(ServiceUnavailableError):
        VectorStore(object())
    assert fake_chromadb.requested == []


def test_vector_store_fails_closed_when_generation_collection_missing(monkeypatch):
    monkeypatch.setattr(
        "gateway.rag.vector_store.read_active_generation", lambda db=None: GEN_1
    )

    class MissingCollection:
        def __call__(self, name, **kwargs):
            raise ValueError(f"Collection {name} does not exist")

    client = types.SimpleNamespace(get_collection=MissingCollection())
    module = types.SimpleNamespace(HttpClient=lambda **kwargs: client)
    monkeypatch.setitem(sys.modules, "chromadb", module)
    with pytest.raises(ServiceUnavailableError):
        VectorStore(object())


# ---------------------------------------------------------------------------
# Real-PostgreSQL concurrency check for lock-first acquire().
#
# No real PostgreSQL is reachable in this environment (no compatible DBAPI
# driver plus no disposable server), so the test is skip-marked rather than
# silently passing against SQLite or fake objects. When a disposable
# PostgreSQL IS available, remove the skip-marker preconditions and the test
# drives the REAL GenerationStore.acquire() from independent processes:
# two workers are released from a common start gate at the same moment and
# contend at schema startup. Only one process acquires the session-level
# advisory lock and proceeds through first-time setup; the other is refused
# by pg_try_advisory_lock BEFORE it executes any DDL. After the winner
# releases, a later acquire succeeds (lock freed on success); after a
# bootstrap/setup failure, a later acquire also succeeds (lock freed on
# failure).
#
# A unique schema is created for this run and torn down in ``finally``.
# Nothing shared, legacy, or live is touched.
# ---------------------------------------------------------------------------


def _real_postgres_url():
    """Connection URL for the disposable concurrency database, or None.

    Deliberately outside the live Compose stack: the caller must point
    PALADIN_PG_CONCURRENCY_URL at a THROWAWAY database. The test never
    touches the live Compose PostgreSQL service or its volumes. A URL whose
    host is the Compose service name is refused outright.
    """
    import os

    return os.environ.get("PALADIN_PG_CONCURRENCY_URL")

def _disposable_schema_name(prefix: str) -> str:
    """Return an owned, disposable schema name for one test run."""
    import uuid

    return f"{prefix}_{uuid.uuid4().hex[:12]}"




def _missing_concurrency_prerequisite():
    """Name the missing real-PostgreSQL prerequisite, or None if all present."""
    import importlib.util

    url = _real_postgres_url()
    if not url:
        return "PALADIN_PG_CONCURRENCY_URL is unset"
    lowered = url.lower()
    for forbidden in ("paladin-postgres", "paladin_postgres", "localhost:5432"):
        if forbidden in lowered:
            return (
                f"PALADIN_PG_CONCURRENCY_URL points at a possibly-live "
                f"database ({forbidden!r}); refusing"
            )
    if importlib.util.find_spec(
        "psycopg2"
    ) is None and importlib.util.find_spec("psycopg") is None:
        return "no compatible PostgreSQL driver installed (need psycopg2 or psycopg)"
    return None


_real_postgres = pytest.mark.skipif(
    _missing_concurrency_prerequisite() is not None,
    reason=(
        "Precondition: disposable PostgreSQL unavailable "
        f"({_missing_concurrency_prerequisite()}); refusing to substitute "
        "SQLite/fakes."
    ),
)

@_real_postgres
def test_acquire_serializes_concurrent_setup_across_processes(tmp_path):
    """Two lock-first workers contend at schema startup: one wins, one refuses.

    Both workers are released from a common start gate at the same moment and
    run ``GenerationStore.acquire()`` against the SAME fresh disposable
    schema. Only one process acquires the advisory lock and proceeds through
    first-time table creation (create_all), column migration and the
    one-active-index replacement; the other is refused by
    pg_try_advisory_lock before executing any DDL. After the winner releases,
    a later acquire succeeds (lock freed on success); after a
    bootstrap/setup failure, a later acquire also succeeds (lock freed on
    failure).
    """
    import json
    import socket
    import subprocess
    import sys
    import time
    from pathlib import Path

    root = str(Path(__file__).resolve().parents[1])
    url = _real_postgres_url()
    schema = _disposable_schema_name("conc_run")
    worker_url = f"{url}?options=-csearch_path%3D{schema}"

    # The disposable schema lives inside the test database itself. Create it
    # here with a connection to the test database (the owning session), rather
    # than in the server's maintenance database, so this test is the only
    # consumer of the schema and the drop below removes exactly what we own.
    test_engine = create_engine(url)
    try:
        with test_engine.begin() as conn:
            conn.execute(sa.text(f"CREATE SCHEMA IF NOT EXISTS {schema}"))
    finally:
        test_engine.dispose()

    # Shared worker template: handshake READY/GO on a local socket, then run
    # a single acquire() and print the outcome as JSON. The parent releases
    # BOTH workers at the same moment, after both have confirmed READY, so
    # the two acquire() calls contend INSIDE lock-first schema startup.
    worker = (
        "import json, socket, sys, time; "
        "sys.path.insert(0, {root!r}); "
        "from sqlalchemy import create_engine; "
        "from gateway.rag.generations import GenerationStore, GenerationError; "
        "store = GenerationStore(create_engine({worker_url!r})); "
        "sock = socket.create_connection(('127.0.0.1', {port})); "
        "sock.settimeout(60); "
        "sock.sendall(b'READY');\n"
        "if sock.recv(1024) != b'GO':\n"
        "    raise SystemExit('gate did not release both workers');\n"
        "sock.close();\n"
        "try:\n"
        "    store.acquire()\n"
        "except GenerationError as exc:\n"
        "    print(json.dumps({{'outcome': 'refused', 'detail': str(exc)}}))\n"
        "else:\n"
        "    time.sleep(5); "
        "    store.release(); "
        "    print(json.dumps({{'outcome': 'held'}}))"
    )

    def _spawn(port):
        return subprocess.Popen(
            [sys.executable, "-c", worker.format(root=root, worker_url=worker_url, port=port)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    # Readiness gate: parent owns a listening socket; each worker connects
    # and signals READY. The parent releases both workers ONLY after both
    # have confirmed they are ready. Bounded timeouts prevent hangs.
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(2)
    port = listener.getsockname()[1]
    listener.settimeout(60)

    first = None
    second = None
    connections = []
    try:
        first = _spawn(port)
        second = _spawn(port)

        # Wait for both workers to confirm readiness, in order.
        for _ in range(2):
            conn, _ = listener.accept()  # bounded by listener.settimeout(60)
            conn.settimeout(60)
            message = conn.recv(1024)
            if message != b"READY":
                raise AssertionError(f"gate message {message!r}, expected READY")
            connections.append(conn)
        listener.close()  # stop further connects; then release both now

        for conn in connections:
            conn.sendall(b"GO")  # release both workers at the same moment
            conn.close()

        first_out, first_err = first.communicate(timeout=120)
        second_out, second_err = second.communicate(timeout=120)

        def _outcome(out, err):
            if out.strip():
                return json.loads(out.strip().splitlines()[-1])
            raise AssertionError(f"worker produced no output; stderr: {err[-2000:]}")

        first_outcome = _outcome(first_out, first_err)
        second_outcome = _outcome(second_out, second_err)

        # --- Phase 1 assertions: exactly one winner, one loser --------------------
        # The race winner is nondeterministic (both workers are released at the
        # same moment), so assert the SET of outcomes rather than assuming the
        # first-spawned worker won.
        outcomes = {first_outcome["outcome"], second_outcome["outcome"]}
        assert outcomes == {"held", "refused"}, (
            f"expected exactly one winner and one loser, got "
            f"first={first_outcome}, second={second_outcome}"
        )
        winner, loser = (
            (first_outcome, second_outcome)
            if first_outcome["outcome"] == "held"
            else (second_outcome, first_outcome)
        )
        loser_err = second_err if loser is second_outcome else first_err
        # The loser was refused BEFORE any DDL: acquire() checks the lock first,
        # THEN runs create_all/migrate/_ensure_one_active_index. It must not
        # have seen a missing-table error or any schema work.
        assert "no such table" not in loser["detail"], loser_err[-2000:]
        assert loser["detail"].startswith(
            "another ingestion holds the generation lock"
        ), loser["detail"]

        # --- Phase 2: later acquire succeeds after a normal release -------------
        # Single-process re-acquire (no gate needed): prove the advisory lock was
        # released after the winner's normal release().
        reacquire_worker = (
            "import json, sys; "
            "sys.path.insert(0, {root!r}); "
            "from sqlalchemy import create_engine; "
            "from gateway.rag.generations import GenerationStore, GenerationError; "
            "store = GenerationStore(create_engine({worker_url!r}));\n"
            "try:\n"
            "    store.acquire()\n"
            "except GenerationError as exc:\n"
            "    print(json.dumps({{'outcome': 'refused', 'detail': str(exc)}}))\n"
            "else:\n"
            "    store.release(); "
            "    print(json.dumps({{'outcome': 'held'}}))"
        )
        reacquire = subprocess.run(
            [sys.executable, "-c", reacquire_worker.format(root=root, worker_url=worker_url)],
            capture_output=True,
            text=True,
            timeout=120,
        )
        reacquire_outcome = _outcome(reacquire.stdout, reacquire.stderr)
        assert (
            reacquire_outcome["outcome"] == "held"
        ), f"lock/connection not released after success: {reacquire.stderr[-2000:]}"

        # --- Phase 3: later acquire succeeds after a bootstrap/setup failure ----
        fail_worker = (
            "import json, sys; "
            "sys.path.insert(0, {root!r}); "
            "from sqlalchemy import create_engine; "
            "from gateway.rag.generations import GenerationStore; "
            "store = GenerationStore(create_engine({worker_url!r})); "
            "store.bootstrap_legacy = lambda name: (_ for _ in ()).throw(RuntimeError('boom'));\n"
            "try:\n"
            "    store.acquire()\n"
            "except RuntimeError:\n"
            "    pass\n"
            "store2 = GenerationStore(create_engine({worker_url!r})); "
            "store2.acquire(); "
            "store2.release(); "
            "print(json.dumps({{'outcome': 'held-after-failure'}}))"
        )
        recovered = subprocess.run(
            [sys.executable, "-c", fail_worker.format(root=root, worker_url=worker_url)],
            capture_output=True,
            text=True,
            timeout=120,
        )
        recovered_outcome = _outcome(recovered.stdout, recovered.stderr)
        assert (
            recovered_outcome["outcome"] == "held-after-failure"
        ), f"lock/connection not released after failure: {recovered.stderr[-2000:]}"

    finally:
        # Bounded cleanup so a failed worker cannot leave the test hung.
        listener.close()
        for proc in (first, second):
            if proc is not None and proc.poll() is None:
                proc.kill()
        # Tear down the disposable schema owned by this run (never a shared
        # one), using a connection to the test database where the schema
        # actually lives. The DROP must run inside a COMMITTED transaction:
        # a bare ``with engine.connect()`` only closes the autobegun
        # transaction, which PostgreSQL then ROLLS BACK, so the schema would
        # survive the test. ``engine.begin()`` commits on exit. The engine is
        # disposed so no pooled session outlives the test, and the schema's
        # absence is asserted HERE — inside ``finally`` — so cleanup is
        # verified even when an earlier phase of this test already failed.
        drop_engine = create_engine(url)
        try:
            with drop_engine.begin() as conn:
                conn.execute(sa.text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
            with drop_engine.connect() as conn:
                surviving = conn.execute(
                    sa.text(
                        "SELECT schema_name FROM information_schema.schemata "
                        "WHERE schema_name = :name"
                    ),
                    {"name": schema},
                ).first()
            assert surviving is None, (
                f"disposable schema {schema!r} still exists after a committed "
                "DROP SCHEMA; the owned schema leaked into the test database"
            )
        finally:
            drop_engine.dispose()


