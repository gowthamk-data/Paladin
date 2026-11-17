"""Isolated tests for safe credential configuration and idempotent seeding."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import gateway.db.seed as seed_mod
from gateway.db.models import Base, User
from gateway.db.seed import SeedConfigurationError
from passlib.hash import bcrypt


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


def _configured_passwords(monkeypatch, *, overrides=None):
    monkeypatch.delenv("PALADIN_ENV", raising=False)
    overrides = overrides or {}
    values = {}
    for username in seed_mod.PASSWORD_ENV_NAMES:
        env_name = seed_mod.PASSWORD_ENV_NAMES[username]
        file_name = f"{env_name}_FILE"
        monkeypatch.delenv(file_name, raising=False)
        value = overrides.get(username, f"test-secret-{username}-12")
        values[username] = value
        monkeypatch.setenv(env_name, value)
    return values


def _by_username(session):
    return {u.username: u for u in session.query(User).all()}


def test_adds_missing_users_using_configured_passwords(monkeypatch):
    configured = _configured_passwords(monkeypatch)
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
            assert bcrypt.verify(configured[user.username], user.password_hash)
    finally:
        session.close()


def test_leaves_existing_attributes_and_password_unchanged(monkeypatch):
    _configured_passwords(monkeypatch)
    factory = _make_session(monkeypatch)
    pre = factory()
    pre.add(
        User(
            username="emp1",
            password_hash="original-hash-must-survive",
            department="OriginalDept",
            clearance_level=3,
            role="original-role",
        )
    )
    pre.commit()
    pre.close()

    seed_mod.seed_users()

    session = factory()
    try:
        users = _by_username(session)
        assert users["emp1"].password_hash == "original-hash-must-survive"
        assert users["emp1"].department == "OriginalDept"
        assert users["emp1"].clearance_level == 3
        assert users["emp1"].role == "original-role"
        assert set(users) == {entry["username"] for entry in seed_mod.SEED_USERS}
    finally:
        session.close()


def test_rerun_is_idempotent(monkeypatch):
    _configured_passwords(monkeypatch)
    factory = _make_session(monkeypatch)
    seed_mod.seed_users()
    first = factory()
    try:
        snapshot = {
            u.username: (u.password_hash, u.department, u.clearance_level, u.role)
            for u in first.query(User).all()
        }
    finally:
        first.close()

    # Different configured secrets must not rotate existing accounts.
    _configured_passwords(monkeypatch, overrides={u: f"changed-secret-{u}-12" for u in seed_mod.PASSWORD_ENV_NAMES})
    seed_mod.seed_users()

    second = factory()
    try:
        assert second.query(User).count() == len(seed_mod.SEED_USERS)
        for user in second.query(User).all():
            assert snapshot[user.username] == (
                user.password_hash,
                user.department,
                user.clearance_level,
                user.role,
            )
    finally:
        second.close()


def test_refuses_missing_configuration_before_database_access(monkeypatch):
    for name in ("PALADIN_ENV", *seed_mod.PASSWORD_ENV_NAMES.values()):
        monkeypatch.delenv(name, raising=False)
    for name in seed_mod.PASSWORD_ENV_NAMES.values():
        monkeypatch.delenv(f"{name}_FILE", raising=False)

    def database_must_not_open():
        raise AssertionError("database accessed before credential validation")

    monkeypatch.setattr(seed_mod, "init_db", database_must_not_open)
    with pytest.raises(SeedConfigurationError, match="PALADIN_SEED_EMP1_PASSWORD"):
        seed_mod.seed_users()


def test_local_dev_requires_explicit_flag_and_allows_builtin_defaults(monkeypatch):
    _configured_passwords(monkeypatch)
    for name in seed_mod.PASSWORD_ENV_NAMES.values():
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(f"{name}_FILE", raising=False)

    with pytest.raises(SeedConfigurationError):
        seed_mod.resolve_seed_passwords()
    assert seed_mod.resolve_seed_passwords(local_dev=True) == seed_mod.LOCAL_DEV_PASSWORDS


def test_production_marker_overrides_local_dev_flag(monkeypatch):
    env = {"PALADIN_ENV": "production"}
    with pytest.raises(SeedConfigurationError, match="disabled"):
        seed_mod.resolve_seed_passwords(local_dev=True, environ=env)


def test_old_default_passwords_are_rejected_outside_local_dev(monkeypatch):
    configured = _configured_passwords(monkeypatch, overrides={"emp1": seed_mod.LOCAL_DEV_PASSWORDS["emp1"]})
    with pytest.raises(SeedConfigurationError, match="only with --local-dev"):
        seed_mod.resolve_seed_passwords()
    assert configured["emp1"] == seed_mod.LOCAL_DEV_PASSWORDS["emp1"]


def test_requires_a_distinct_secret_for_each_account(monkeypatch):
    shared = "shared-but-long-enough-secret"
    _configured_passwords(
        monkeypatch,
        overrides={username: shared for username in seed_mod.PASSWORD_ENV_NAMES},
    )
    with pytest.raises(SeedConfigurationError, match="distinct credential"):
        seed_mod.resolve_seed_passwords()


@pytest.mark.parametrize("byte_length", [11, 73])
def test_rejects_passwords_outside_utf8_byte_limits(monkeypatch, byte_length):
    value = "x" * byte_length
    _configured_passwords(monkeypatch, overrides={"emp1": value})
    with pytest.raises(SeedConfigurationError, match="12 and 72 UTF-8 bytes"):
        seed_mod.resolve_seed_passwords()


def test_accepts_password_at_utf8_byte_boundaries(monkeypatch):
    lower = "é" * 6  # 12 UTF-8 bytes
    upper = "x" * 72
    _configured_passwords(monkeypatch, overrides={"emp1": lower, "admin1": upper})
    resolved = seed_mod.resolve_seed_passwords()
    assert resolved["emp1"] == lower
    assert resolved["admin1"] == upper


def test_reads_secret_file_and_rejects_both_sources(monkeypatch, tmp_path):
    values = _configured_passwords(monkeypatch)
    secret_file = tmp_path / "admin1-secret"
    secret_file.write_text("a-different-admin-secret\n", encoding="utf-8")
    admin_env = seed_mod.PASSWORD_ENV_NAMES["admin1"]
    monkeypatch.delenv(admin_env)
    monkeypatch.setenv(f"{admin_env}_FILE", str(secret_file))
    resolved = seed_mod.resolve_seed_passwords()
    assert resolved["admin1"] == "a-different-admin-secret"
    assert resolved["admin1"] != values["admin1"]

    monkeypatch.setenv(admin_env, values["admin1"])
    with pytest.raises(SeedConfigurationError, match="not both"):
        seed_mod.resolve_seed_passwords()


def test_configuration_errors_never_include_secret_values(monkeypatch, capsys):
    secret = "private-test-secret-value"
    _configured_passwords(monkeypatch, overrides={"emp1": secret})
    monkeypatch.setenv("PALADIN_ENV", "production")
    assert seed_mod.main(["--local-dev"]) == 2
    captured = capsys.readouterr()
    assert secret not in captured.out + captured.err
