"""Safely create missing demo users from explicitly configured credentials.

Production seeding requires four distinct password values supplied through
environment variables or mounted secret files. The built-in demonstration
passwords are available only with an explicit ``--local-dev`` flag.
"""

import argparse
import os
import sys
from pathlib import Path
from typing import Mapping

from passlib.hash import bcrypt

from gateway.db.session import init_db
from gateway.db.models import User


# User attributes are policy fixtures; credentials are deliberately configured
# separately so production seeding cannot silently use demo defaults.
SEED_USERS = [
    {"username": "emp1", "department": "Engineering", "clearance_level": 1, "role": "employee"},
    {"username": "mgr1", "department": "Engineering", "clearance_level": 2, "role": "manager"},
    {"username": "admin1", "department": "Executive", "clearance_level": 3, "role": "admin"},
    {"username": "fin1", "department": "Finance", "clearance_level": 2, "role": "employee"},
]

# These short, publicly documented passwords are for throwaway local demos
# only. They are rejected unless the operator explicitly passes --local-dev.
LOCAL_DEV_PASSWORDS = {
    "emp1": "emp1_pass",
    "mgr1": "mgr1_pass",
    "admin1": "admin1_pass",
    "fin1": "fin1_pass",
}

PASSWORD_ENV_NAMES = {
    username: f"PALADIN_SEED_{username.upper()}_PASSWORD"
    for username in LOCAL_DEV_PASSWORDS
}


class SeedConfigurationError(ValueError):
    """Raised when credentials are missing or violate the seeder policy."""


def _configured_password(username: str, environ: Mapping[str, str], local_dev: bool) -> str:
    env_name = PASSWORD_ENV_NAMES[username]
    file_name = f"{env_name}_FILE"
    direct_value = environ.get(env_name) or None
    file_path = environ.get(file_name) or None

    if direct_value and file_path:
        raise SeedConfigurationError(
            f"Configure either {env_name} or {file_name}, not both."
        )

    if file_path:
        try:
            password = Path(file_path).read_text(encoding="utf-8").rstrip("\r\n")
        except (OSError, UnicodeError) as exc:
            raise SeedConfigurationError(
                f"Could not read the configured secret file for {env_name}."
            ) from exc
    elif direct_value:
        password = direct_value
    elif local_dev:
        password = LOCAL_DEV_PASSWORDS[username]
    else:
        raise SeedConfigurationError(
            f"Missing required credential: set {env_name} or {file_name}."
        )

    if not password:
        raise SeedConfigurationError(f"Credential {env_name} must not be empty.")

    if password == LOCAL_DEV_PASSWORDS[username] and not local_dev:
        raise SeedConfigurationError(
            f"The built-in demo credential for {username} is permitted only with --local-dev."
        )

    password_bytes = len(password.encode("utf-8"))
    # The short built-in values are the sole length exception, and are reachable
    # only through the explicit local-development mode.
    if not (local_dev and password == LOCAL_DEV_PASSWORDS[username]):
        if not 12 <= password_bytes <= 72:
            raise SeedConfigurationError(
                f"Credential {env_name} must be between 12 and 72 UTF-8 bytes."
            )
    return password


def resolve_seed_passwords(
    local_dev: bool = False,
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Validate and return configured passwords without logging their values."""
    env = os.environ if environ is None else environ
    is_production = env.get("PALADIN_ENV", "").strip().casefold() == "production"
    if is_production and local_dev:
        raise SeedConfigurationError(
            "--local-dev is disabled when PALADIN_ENV=production."
        )

    passwords = {
        username: _configured_password(username, env, local_dev)
        for username in PASSWORD_ENV_NAMES
    }
    if len(set(passwords.values())) != len(passwords):
        raise SeedConfigurationError(
            "Each demo account must have a distinct credential."
        )
    return passwords


def seed_users(local_dev: bool = False):
    """Create missing users; never update an existing account.

    Credentials are fully validated before database initialization or writes.
    Existing rows are skipped without changing their hash or attributes.
    """
    passwords = resolve_seed_passwords(local_dev=local_dev)
    db = init_db()
    try:
        created = 0
        for user_data in SEED_USERS:
            username = user_data["username"]
            existing = db.query(User).filter(User.username == username).first()
            if existing is not None:
                print(f"[SEED] {username} already exists, skipping.")
                continue
            user = User(
                username=username,
                password_hash=bcrypt.hash(passwords[username]),
                department=user_data["department"],
                clearance_level=user_data["clearance_level"],
                role=user_data["role"],
            )
            db.add(user)
            created += 1
            print(
                f"[SEED] Created user: {username} "
                f"(clearance: {user_data['clearance_level']}, role: {user_data['role']})"
            )
        db.commit()
        print(f"[SEED] Seeding complete. {created} created, {len(SEED_USERS) - created} already existed.")
    finally:
        db.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create missing Paladin demo users safely.")
    parser.add_argument(
        "--local-dev",
        action="store_true",
        help="allow built-in demo passwords for a throwaway local development database",
    )
    args = parser.parse_args(argv)
    try:
        seed_users(local_dev=args.local_dev)
    except SeedConfigurationError as exc:
        print(f"[SEED] Refused: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
