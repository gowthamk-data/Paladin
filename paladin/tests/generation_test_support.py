"""Shared doubles for the generation-cutover test suites.

``SQLiteGenerationStore`` runs the REAL pointer logic (register / activate /
rollback / forget / cleanup_candidates / bootstrap_legacy) of
``gateway.rag.generations.GenerationStore`` against an in-memory SQLite
database. Only the PostgreSQL-only lifecycle bits are replaced:

* ``acquire()`` normally creates the table, takes a session-level advisory
  lock and enforces the ``postgresql`` dialect; SQLite has neither, so the
  test subclass creates the table and opens a connection directly.
* ``release()`` normally issues ``pg_advisory_unlock``; the subclass just
  closes the connection.

Everything that decides *which generation is active* is production code.
The one-active partial unique index (``ONE_ACTIVE_INDEX_DDL``) is created by
the real ``acquire()`` on PostgreSQL; the subclass executes the same DDL on
SQLite — SQLite supports the identical partial-index syntax — so the
single-active invariant is enforced by the DATABASE in these tests too, not
merely by ``activate()`` demoting the current active row in the same
transaction that promotes the new one.
"""

from __future__ import annotations

import sqlalchemy as sa

from gateway.db.models import Base, GenerationRow
from gateway.rag.generations import (
    ONE_ACTIVE_INDEX_DDL,
    GenerationError,
    GenerationStore,
)


class SQLiteGenerationStore(GenerationStore):
    """Real GenerationStore semantics on an in-memory SQLite database."""

    def __init__(self, engine, legacy_name: str = "paladin_docs_abac"):
        super().__init__(engine=engine)
        self.legacy_name = legacy_name

    def acquire(self) -> None:
        if self._conn is not None:
            raise GenerationError("generation store is already acquired")
        Base.metadata.create_all(bind=self.engine, tables=[GenerationRow.__table__])
        self._conn = self.engine.connect()
        # Mirror the real acquire(): migrate columns (each ALTER in its own
        # transaction) and create the one-active partial unique index (SQLite
        # supports the same partial-index DDL), so the single-active invariant
        # is enforced by the DATABASE here too — not only by activate()'s
        # transaction. Then adopt the legacy collection once.
        self._migrate_columns(self._conn)
        with self._conn.begin():
            self._ensure_one_active_index(self._conn)
        self.bootstrap_legacy(self.legacy_name)

    def release(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            conn.close()


class FakeChromaCollection:
    """Dict-backed stand-in for a Chroma collection (ids -> row)."""

    def __init__(self):
        self.store: dict[str, dict] = {}

    def upsert(self, **kwargs):
        for index, doc_id in enumerate(kwargs["ids"]):
            self.store[doc_id] = {
                "ids": doc_id,
                "documents": kwargs["documents"][index],
                "embeddings": kwargs["embeddings"][index],
                "metadatas": kwargs["metadatas"][index],
            }

    def get(self, include=None):
        ids = list(self.store)
        out = {
            "ids": ids,
            "documents": [self.store[i]["documents"] for i in ids],
            "metadatas": [self.store[i]["metadatas"] for i in ids],
        }
        if include is None or "embeddings" in include:
            out["embeddings"] = [self.store[i]["embeddings"] for i in ids]
        return out

    def delete(self, ids):
        for doc_id in ids:
            self.store.pop(doc_id, None)


class FakeChromaClient:
    """In-memory stand-in for chromadb.HttpClient's collection registry.

    Mirrors chromadb 0.4.24 behaviour relevant to cutover, as verified against
    a disposable 0.4.24 instance: a missing collection raises a *plain
    Exception* whose message is the serialised server error (the unreachable
    server is the case that raises ``ValueError``), and creating an existing
    collection raises ValueError "already exists".
    """

    def __init__(self):
        self.collections: dict[str, FakeChromaCollection] = {}

    def create_collection(self, name, metadata=None):
        if name in self.collections:
            raise ValueError(f"Collection {name} already exists")
        collection = FakeChromaCollection()
        self.collections[name] = collection
        return collection

    def delete_collection(self, name):
        self.get_collection(name=name)
        del self.collections[name]

    def get_collection(self, name, **kwargs):
        if name not in self.collections:
            # Mirror chromadb 0.4.24 EXACTLY, as verified against a disposable
            # 0.4.24 instance: a missing collection raises a *plain Exception*
            # wrapping the serialised server error, while an unreachable server
            # is what raises ValueError. Getting these the wrong way round is a
            # real fail-open bug, so the fake reproduces both shapes.
            raise Exception(
                '{"error":"ValueError(\'Collection %s does not exist.\')"}' % name
            )
        return self.collections[name]

    def list_collections(self):
        return list(self.collections.values())

    def raise_unreachable(self):
        """Simulate the 0.4.24 unreachable-server driver error."""

        def _dead(name, **kwargs):
            raise ValueError(
                "Could not connect to a Chroma server. Are you sure it is running?"
            )

        self.get_collection = _dead
