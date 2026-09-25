from passlib.hash import bcrypt


def hash_password(password: str) -> str:
    return bcrypt.hash(password)


def verify_password(plain_password: str, password_hash: str) -> bool:
    return bcrypt.verify(plain_password, password_hash)
