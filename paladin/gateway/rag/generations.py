"""Authoritative active-generation pointer for the Chroma vector index.

The gateway never writes to a collection it serves. Every ingestion builds a
fresh, versioned Chroma collection (``<CHROMA_COLLECTION>_gen_<NNNNNN>``),
validates it, and only then flips the pointer — the ``is_active`` row in the
``vector_generations`` table — in a single PostgreSQL transaction, while
holding a session-level advisory lock that serialises ingestions. Queries
always resolve to exactly one fully built generation; the most recently
deactivated generation is retained as the rollback target, and the
pre-versioning legacy collection is retained as the bootstrap fallback.

ChromaDB 0.4.24 (pinned) offers no atomic rename/alias/swap primitive, so the
pointer indirection in PostgreSQL 16 is what makes cutover atomic: the build
happens while the new collection is invisible to the pointer, and the flip is
the single mutation a query can observe.
"""

from __future__ import annotations

import re
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as postgresql_dialect
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from gateway.config import CHROMA_COLLECTION
from gateway.db.models import Base, GenerationRow

GENERATION_PREFIX = f"{CHROMA_COLLECTION}_gen_"
_GENERATION_SUFFIX = re.compile(rf"^{re.escape(GENERATION_PREFIX)}(\d+)$")

# Session-level advisory-lock key serialising ingestion and cleanup. Any value
# works as long as it is stable and unique to this purpose within the database.
INGEST_LOCK_KEY = 0x50414C47  # "PALG"

# Partial unique index enforcing AT MOST ONE active generation. Every row the
# index contains has ``is_active = true``, so uniqueness on the flag itself
# admits exactly one active row across all collection names.
# ``GenerationStore._ensure_one_active_index`` replaces legacy name-based indices
# with this flag-based index.
# DDL types are rendered against the PostgreSQL dialect: every type the
# pointer model uses (VARCHAR(255), BOOLEAN, TIMESTAMP) is valid on both
# PostgreSQL (production) and SQLite (the isolated tests), while the generic
# rendering of DateTime — DATETIME — is rejected by PostgreSQL. Compiling
# against a fixed dialect also keeps the migration independent of the engine
# object, so it cannot depend on a caller's dialect internals.
_DDL_DIALECT = postgresql_dialect.dialect()

ONE_ACTIVE_INDEX_DDL = (
    "CREATE UNIQUE INDEX IF NOT EXISTS vector_generations_one_active "
    "ON vector_generations (is_active) WHERE is_active"
)


class GenerationError(RuntimeError):
    """Pointer-store failure: lock held, corrupt state, or no rollback target."""


def _is_duplicate_column(exc: BaseException) -> bool:
    """True only for the "column already exists" ALTER TABLE failure.

    Identified by driver error code first, never by message substring: the
    wrapped driver error's SQLSTATE must be ``42701``
    (``duplicate_column`` on psycopg2/psycopg). Drivers that do not report
    SQLSTATE (notably SQLite, whose ``sqlite3.OperationalError`` has no
    ``pgcode``) fall back to the driver's own "duplicate column name"
    message. Any other failure — a missing table, a permission error — is not
    a duplicate and must propagate so a broken schema fails closed.
    """
    orig = getattr(exc, "orig", None)
    pgcode = getattr(orig, "pgcode", None)
    if pgcode is not None:
        return pgcode == "42701"
    text = str(orig if orig is not None else exc).lower()
    return "duplicate column name" in text


def read_active_generation(db: Session | None = None) -> str | None:
    """Return the Chroma collection name of the authoritative active generation.

    ``None`` means "no pointer yet" — a table that does not exist yet, or an
    existing but empty table (a deployment that has not run a versioned
    ingestion). The gateway then falls back to the legacy collection name. A
    table that HAS rows but NO active row is corrupt state (a crashed flip or
    manual edit): that raises ``GenerationError`` so the caller fails closed
    instead of silently serving stale legacy data. Any other database failure
    propagates so the caller fails closed.
    """
    close = False
    if db is None:
        from gateway.db.session import SessionLocal

        db = SessionLocal()
        close = True
    try:
        # Do not discover the bootstrap case by querying a missing table. On
        # PostgreSQL, undefined_table aborts the current transaction; catching
        # that exception and returning None would leave the request's shared
        # Session unusable for its later request_logs commit. Catalog
        # inspection is read-only and returns False without poisoning the
        # transaction, allowing the pre-versioning legacy fallback.
        connection = db.connection()
        if not sa.inspect(connection).has_table(GenerationRow.__tablename__):
            return None

        active_rows = (
            db.query(GenerationRow)
            .filter(GenerationRow.is_active.is_(True))
            .order_by(GenerationRow.activated_at.desc())
            .all()
        )
        if len(active_rows) > 1:
            # Corrupt state, not a newest-wins race: the one-active index makes
            # this impossible in a healthy deployment, but a manually edited or
            # partially migrated table must never silently resolve to one of
            # the rows — fail closed so the gateway returns 503.
            names = ", ".join(row.name for row in active_rows)
            raise GenerationError(
                "pointer state corrupt: multiple active generations "
                f"({names}); the one-active constraint is violated"
            )
        if active_rows:
            return active_rows[0].name
        # No active row: an empty table is "no pointer yet", but any surviving
        # row without an active flag is corrupt — never serve legacy silently.
        if db.query(GenerationRow.name).limit(1).first() is not None:
            raise GenerationError(
                "pointer state corrupt: rows exist but none is active"
            )
        return None
    finally:
        if close:
            db.close()


class GenerationStore:
    """Mutating operations on the pointer, ingest-side only.

    All operations run on one dedicated connection held between ``acquire()``
    and ``release()``. ``acquire()`` takes the ingestion advisory lock FIRST
    and only then creates the table if needed, migrates columns onto an
    existing table and (re)creates the one-active index — so concurrent
    ingestions can never race DDL against each other, and a setup failure
    releases the lock and the connection explicitly (a pooled connection that
    is merely closed would keep the session lock and block every later run).
    The legacy collection is adopted as the active generation on first use
    (but only when that collection actually exists in Chroma — a probe the
    caller supplies); concurrent ingestions are refused immediately.

    The store deliberately knows nothing about Chroma: collection lifecycle
    (create, delete) belongs to the caller, which keeps Chroma and PostgreSQL
    ordering explicit — build first, flip last, clean up after. The only
    Chroma knowledge the store accepts is the ``collection_exists`` predicate,
    injected so the pointer module never imports the Chroma client: ingest
    passes a ``get_collection`` wrapper, tests pass a dict lookup, and
    ``None`` preserves assume-present behaviour for callers that already
    verified existence.
    """

    def __init__(self, engine=None, collection_exists=None):
        if engine is None:
            from gateway.db.session import engine as default_engine

            engine = default_engine
        self.engine = engine
        self.collection_exists = collection_exists
        self._conn = None

    # -- lifecycle ---------------------------------------------------------

    def acquire(self) -> None:
        if self._conn is not None:
            raise GenerationError("generation store is already acquired")
        if self.engine.dialect.name != "postgresql":
            raise GenerationError("generation pointer store requires PostgreSQL")
        conn = self.engine.connect()
        # The advisory lock is taken BEFORE any schema work, so two
        # concurrent ingestions can never race DDL against each other: the
        # loser of pg_try_advisory_lock is refused immediately and never
        # touches the table, the index, or the migration. The lock is
        # session-level and survives the commit that closes the autobegun
        # transaction the SELECT started.
        lock_acquired = False
        try:
            acquired = conn.execute(
                sa.text("SELECT pg_try_advisory_lock(:key)"),
                {"key": INGEST_LOCK_KEY},
            ).scalar()
            # Close the autobegun transaction so the next conn.begin() can
            # start; the session lock itself persists across the commit.
            conn.commit()
            lock_acquired = bool(acquired)
            if not acquired:
                raise GenerationError(
                    "another ingestion holds the generation lock; "
                    "refusing concurrent runs"
                )
            # Under the lock: create the table if this is a first-time
            # deployment, migrate columns onto an existing table, and
            # (re)create the one-active index. Per-column transactions: a
            # swallowed duplicate-column failure must not poison a
            # surrounding transaction (PostgreSQL aborts the whole
            # transaction on error), so each ALTER commits or rolls back
            # alone before the index is (re)created.
            Base.metadata.create_all(bind=self.engine, tables=[GenerationRow.__table__])
            self._migrate_columns(conn)
            with conn.begin():
                self._ensure_one_active_index(conn)
        except BaseException:
            if lock_acquired:
                # Release the lock explicitly: conn.close() alone would
                # return the connection to the pool STILL HOLDING the
                # session lock, and the next pool borrower would inherit it
                # and be refused forever. If the unlock cannot run (broken
                # connection), invalidate() discards the connection so the
                # backend session ends and the lock dies with it.
                try:
                    conn.execute(
                        sa.text("SELECT pg_advisory_unlock(:key)"),
                        {"key": INGEST_LOCK_KEY},
                    )
                    conn.commit()
                except BaseException:
                    conn.invalidate()
            conn.close()
            raise
        self._conn = conn
        try:
            self.bootstrap_legacy(CHROMA_COLLECTION)
        except BaseException:
            # Bootstrap manages its own per-branch commits, so nothing here
            # rolls pointer state back — but the advisory lock MUST be given
            # up and the connection closed, otherwise a retried ingestion
            # finds the lock still held on a broken connection and refuses
            # to run. self.release() both unlocks and closes; it cannot fail
            # the cleanup path because its body is itself guarded.
            self.release()
            raise

    def release(self) -> None:
        conn, self._conn = self._conn, None
        if conn is None:
            return
        try:
            conn.execute(
                sa.text("SELECT pg_advisory_unlock(:key)"), {"key": INGEST_LOCK_KEY}
            )
            # Commit the (autobegun) transaction around the unlock so the
            # connection closes clean; the session lock is already released
            # server-side by the function call itself.
            conn.commit()
        except BaseException:
            # The unlock never reached the server, or the commit failed, so
            # this backend session MAY STILL HOLD the session-level advisory
            # lock. A plain conn.close() would hand that connection straight
            # back to the pool STILL HOLDING the lock, and the next borrower
            # would inherit it and be refused forever. invalidate() discards
            # the connection instead, so the backend session ends and the
            # lock dies with it; close() is then a harmless no-op.
            #
            # Both cleanup steps are individually guarded so a failing
            # invalidate()/close() can never mask the original unlock/commit
            # failure — that exception is what the caller must see, and the
            # bare ``raise`` below re-raises it unchanged.
            try:
                conn.invalidate()
            except BaseException:
                pass
            try:
                conn.close()
            except BaseException:
                pass
            raise
        conn.close()

    def _require_conn(self):
        if self._conn is None:
            raise GenerationError("generation store is not acquired")
        return self._conn

    def _migrate_columns(self, conn) -> None:
        """Add columns introduced after the first versioned deployment.

        ``Base.metadata.create_all`` only creates tables that are missing
        entirely; it never alters an existing table, so a pointer table from
        an earlier deployment would not gain ``deactivated_at`` — and cleanup
        would read a column that does not exist. Every model column is added
        if absent; "already exists" is identified by driver error code
        (PostgreSQL SQLSTATE 42701, SQLite's own duplicate-column message),
        using driver error codes rather than a bare message match on
        PostgreSQL. Each ALTER runs in its own transaction so a swallowed
        duplicate cannot poison the next one, and any other failure (missing
        table, permissions) propagates so a broken schema fails closed.
        """
        for column in GenerationRow.__table__.columns:
            try:
                with conn.begin():
                    conn.execute(
                        sa.text(
                            f"ALTER TABLE vector_generations "
                            f"ADD COLUMN {column.name} "
                            f"{column.type.compile(dialect=_DDL_DIALECT)}"
                        )
                    )
            except SQLAlchemyError as exc:
                if not _is_duplicate_column(exc):
                    raise

    def _ensure_one_active_index(self, conn) -> None:
        """(Re)create the one-active partial unique index.

        An index of the original ``UNIQUE (name) WHERE is_active`` shape is
        dropped and replaced — it does not enforce the invariant. If the
        existing table already holds multiple active rows the new index cannot
        be created: that is deliberate fail-closed behaviour, and the operator
        must resolve the corrupt state before ingestion runs again. Must run
        inside a transaction the caller owns.
        """
        if self.engine.dialect.name == "postgresql":
            row = conn.execute(
                sa.text(
                    "SELECT indexdef FROM pg_indexes "
                    "WHERE indexname = 'vector_generations_one_active'"
                )
            ).first()
            if row is not None and "(name) WHERE" in row[0]:
                conn.execute(sa.text("DROP INDEX vector_generations_one_active"))
        conn.execute(sa.text(ONE_ACTIVE_INDEX_DDL))

    # -- reads -------------------------------------------------------------

    # The two private helpers take an open connection and MUST be called with
    # a transaction already open on it; the public wrappers supply one.
    @staticmethod
    def _active_name(conn) -> str | None:
        row = (
            conn.execute(
                sa.select(GenerationRow.name)
                .where(GenerationRow.is_active.is_(True))
                .order_by(GenerationRow.activated_at.desc())
                .limit(1)
            )
            .first()
        )
        return row[0] if row else None

    @staticmethod
    def _previous_name(conn) -> str | None:
        """Most recently deactivated generation: the rollback target."""
        row = (
            conn.execute(
                sa.select(GenerationRow.name)
                .where(
                    GenerationRow.is_active.is_(False),
                    GenerationRow.activated_at.is_not(None),
                )
                .order_by(GenerationRow.activated_at.desc())
                .limit(1)
            )
            .first()
        )
        return row[0] if row else None

    def active_name(self) -> str | None:
        # Must be transaction-scoped: on a SQLAlchemy 2.x connection a bare
        # execute() autobegins a transaction that would still be open when a
        # later mutation tries to begin() its own.
        with self._require_conn().begin():
            return self._active_name(self._require_conn())

    def previous_name(self) -> str | None:
        with self._require_conn().begin():
            return self._previous_name(self._require_conn())

    def next_generation_name(self) -> str:
        # Must be transaction-scoped: on a SQLAlchemy 2.x connection a bare
        # execute() autobegins a transaction that would still be open when
        # register() tries to begin() its own.
        with self._require_conn().begin():
            rows = self._require_conn().execute(
                sa.select(GenerationRow.name)
            ).all()
        highest = 0
        for (name,) in rows:
            match = _GENERATION_SUFFIX.match(name)
            if match:
                highest = max(highest, int(match.group(1)))
        return f"{GENERATION_PREFIX}{highest + 1:06d}"

    def cleanup_candidates(self, in_use_grace_seconds: float = 0.0) -> list[str]:
        """Row names whose collections are outside the retention set.

        Retention: the active generation, the rollback target, and the legacy
        bootstrap collection are kept. Everything else — older generations and
        the leftover rows/collections of crashed runs — is removable, EXCEPT
        for a generation whose ``deactivated_at`` is less than
        ``in_use_grace_seconds`` ago:
        the gateway resolves the pointer per request and can still be serving
        a request that opened the previous generation just before a cutover
        (embedding + Chroma query + generation), so a just-deactivated
        collection is held for that window instead of being deleted out from
        under an in-flight request. Older-than-grace generations are removable.

        Safe to call only under the ingestion advisory lock, which guarantees
        no other run has a half-built generation in flight.
        """
        with self._require_conn().begin():
            conn = self._require_conn()
            rows = conn.execute(
                sa.select(GenerationRow.name, GenerationRow.deactivated_at)
            ).all()
            keep = {
                name
                for name in (
                    self._active_name(conn),
                    self._previous_name(conn),
                    CHROMA_COLLECTION,
                )
                if name
            }
        candidates = []
        for name, deactivated_at in rows:
            if name in keep:
                continue
            # A generation deactivated within the grace window may still be
            # mid-request: keep it this pass. The window runs from
            # DEACTIVATION — the timestamp recorded at cutover — not from
            # activation, so a generation that served for weeks still gets
            # its full grace period after losing the pointer instead of being
            # reclaimable the moment it is superseded. A generation that was
            # never active (deactivated_at NULL) has no grace protection.
            if (
                in_use_grace_seconds > 0
                and deactivated_at is not None
                and (datetime.utcnow() - deactivated_at).total_seconds()
                < in_use_grace_seconds
            ):
                continue
            candidates.append(name)
        return candidates

    # -- mutations ---------------------------------------------------------

    def register(self, name: str) -> None:
        """Record a new, not-yet-active generation before it is built."""
        with self._require_conn().begin():
            self._require_conn().execute(
                sa.insert(GenerationRow).values(name=name, is_active=False)
            )

    def activate(self, name: str) -> None:
        """Atomically flip the pointer to a fully built generation.

        One transaction: demote the current active row (its ``activated_at``
        is preserved so it remains the rollback target, and it gains a
        ``deactivated_at`` timestamp that starts its in-use grace window),
        then activate ``name``. If ``name`` is not a registered generation,
        the transaction rolls back and the pointer is untouched.
        """
        with self._require_conn().begin():
            conn = self._require_conn()
            conn.execute(
                sa.update(GenerationRow)
                .where(GenerationRow.is_active.is_(True))
                .values(is_active=False, deactivated_at=datetime.utcnow())
            )
            result = conn.execute(
                sa.update(GenerationRow)
                .where(GenerationRow.name == name)
                .values(is_active=True, activated_at=datetime.utcnow())
            )
            if result.rowcount != 1:
                raise GenerationError(
                    f"generation {name!r} is not registered; refusing to activate"
                )

    def rollback(self) -> str:
        """Swap the pointer back to the previous (most recently deactivated) generation.

        The rollback target's Chroma collection is verified first (when a
        ``collection_exists`` probe was supplied): flipping to a generation
        whose collection is gone would take serving from degraded to dead.
        A missing target stays registered so a restored collection can still
        be rolled back to; the error tells the operator exactly that.
        """
        previous = self.previous_name()
        if previous is None:
            raise GenerationError("no previous generation to roll back to")
        if self.collection_exists is not None and not self.collection_exists(previous):
            raise GenerationError(
                f"refusing to roll back to {previous!r}: its collection is "
                "missing from Chroma; restore the collection first"
            )
        self.activate(previous)
        return previous

    def forget(self, name: str) -> None:
        """Drop a non-active generation's row (never the active one)."""
        with self._require_conn().begin():
            conn = self._require_conn()
            result = conn.execute(
                sa.delete(GenerationRow).where(
                    GenerationRow.name == name,
                    GenerationRow.is_active.is_(False),
                )
            )
            if result.rowcount != 1 and self._active_name(conn) == name:
                raise GenerationError(
                    f"refusing to forget the active generation {name!r}"
                )

    # -- bootstrap ---------------------------------------------------------

    def bootstrap_legacy(self, legacy_name: str) -> str | None:
        """Adopt the pre-versioning collection as the active generation once.

        Idempotent: a no-op returning the current active name whenever any
        pointer row already exists. On a fresh install the legacy collection
        is adopted ONLY if it actually exists in Chroma (checked via the
        ``collection_exists`` probe); when the probe is absent, presence is
        assumed, preserving the old behaviour for callers that verified it
        themselves. Returns ``None`` when there is no legacy collection to
        adopt — the first versioned ingestion then builds gen_000001 and
        activates it directly.
        """
        conn = self._require_conn()
        with conn.begin():
            existing = conn.execute(sa.select(GenerationRow.name).limit(1)).first()
            if existing is None:
                if self.collection_exists is not None and not self.collection_exists(
                    legacy_name
                ):
                    return None
                conn.execute(
                    sa.insert(GenerationRow).values(
                        name=legacy_name,
                        is_active=True,
                        activated_at=datetime.utcnow(),
                    )
                )
                return legacy_name
            active = self._active_name(conn)
            if active is None:
                raise GenerationError(
                    "pointer state corrupt: rows exist but none is active"
                )
            return active
