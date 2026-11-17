"""Resolve test credentials without hard-coded demo-password fallbacks."""

import os
from collections.abc import Mapping


def password_for(username: str, environ: Mapping[str, str] | None = None) -> str:
    """Return a user password from PALADIN_TEST_<USER>_PASSWORD.

    Error messages name the missing variable but never display its value.
    """
    env = os.environ if environ is None else environ
    if username not in {"emp1", "mgr1", "admin1", "fin1"}:
        raise ValueError("Unknown demo user requested by live test.")
    variable = f"PALADIN_TEST_{username.upper()}_PASSWORD"
    password = env.get(variable)
    if not password:
        raise RuntimeError(f"Set {variable} in the test process environment.")
    return password
