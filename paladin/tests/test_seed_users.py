"""Isolated unit tests for seed_users() — no live database required.

Uses an in-memory SQLite database and monkeypatches seed.init_db so the
real Postgres/session machinery is never touched. Verifies the three
contract properties documented in seed.seed_users():
  1. missing demo accounts are added,
  2. existing rows are left entirely unchanged (attributes AND password hash),
  3. reruns are idempotent.
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import gateway.db.seed as seed_mod
from gateway.db.models import Base, User


def _make_session(monkeypatch):
    # Models use Postgres UUID columns, which SQLite cannot render. Swap them
    # for CHAR(36) just long enough to create the schema, then restore.
    from sqlalchemy import CHAR, TypeDecorator

    class UuidStr(TypeDecorator):
        impl = CHAR(36)
        cache_ok = True

        def process_bind_param(self, value, dialect):
            return None if value is None else str(value)

        def process_result_value(self, value, dialect):
            return value

    originals = []
    for table in Base.metadata.tables.values():
        for col in table.columns:
            if col.type.__class__.__name__ == "UUID":
                originals.append((col, col.type))
                col.type = UuidStr()
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    try:
        Base.metadata.create_all(bind=engine)
    finally:
        for col, original in originals:
            col.type = original
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    monkeypatch.setattr(seed_mod, "init_db", lambda: factory())
    return factory


def _by_username(session):
    return {u.username: u for u in session.query(User).all()}


def test_adds_missing_users_when_empty(monkeypatch):
    factory = _make_session(monkeypatch)
    seed_mod.seed_users()
    session = factory()
    try:
        users = _by_username(session)
        assert set(users) == {entry["username"] for entry in seed_mod.SEED_USERS}
        for entry in seed_mod.SEED_USERS:
            user = users[entry["username"]]
            assert user.department == entry["department"]
            assert user.clearance_level == entry["clearance_level"]
            assert user.role == entry["role"]
            assert user.password_hash and user.password_hash != entry["password"]
    finally:
        session.close()


def test_leaves_existing_attributes_and_password_unchanged(monkeypatch):
    factory = _make_session(monkeypatch)
    pre = factory()
    sentinel = User(
        username="emp1",
        password_hash="original-hash-must-survive",
        department="OriginalDept",
        clearance_level=3,
        role="original-role",
    )
    pre.add(sentinel)
    pre.commit()
    pre.close()

    seed_mod.seed_users()

    session = factory()
    try:
        users = _by_username(session)
        # Pre-existing emp1 untouched, all other seed users added.
        assert users["emp1"].password_hash == "original-hash-must-survive"
        assert users["emp1"].department == "OriginalDept"
        assert users["emp1"].clearance_level == 3
        assert users["emp1"].role == "original-role"
        assert set(users) == {entry["username"] for entry in seed_mod.SEED_USERS}
    finally:
        session.close()


def test_rerun_is_idempotent(monkeypatch):
    factory = _make_session(monkeypatch)
    seed_mod.seed_users()
    first = factory()
    try:
        snapshot = {
            u.username: (u.password_hash, u.department, u.clearance_level, u.role)
            for u in first.query(User).all()
        }
        count_before = first.query(User).count()
    finally:
        first.close()

    seed_mod.seed_users()

    second = factory()
    try:
        assert second.query(User).count() == count_before == len(seed_mod.SEED_USERS)
        for user in second.query(User).all():
            assert snapshot[user.username] == (
                user.password_hash,
                user.department,
                user.clearance_level,
                user.role,
            )
    finally:
        second.close()
