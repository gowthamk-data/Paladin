import os
import uuid
from datetime import datetime, timedelta
from jose import jwt, JWTError
from gateway.config import (
    JWT_PRIVATE_KEY_PATH,
    JWT_PUBLIC_KEY_PATH,
    JWT_ALGORITHM,
    JWT_EXPIRY_MINUTES,
)

with open(JWT_PRIVATE_KEY_PATH, "r") as f:
    PRIVATE_KEY = f.read()

with open(JWT_PUBLIC_KEY_PATH, "r") as f:
    PUBLIC_KEY = f.read()

ISSUER = "paladin"
AUDIENCE = "paladin-gateway"


def create_access_token(data: dict, expires_minutes: int = None) -> str:
    if expires_minutes is None:
        expires_minutes = JWT_EXPIRY_MINUTES
    to_encode = data.copy()
    now = datetime.utcnow()
    to_encode.update({
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=expires_minutes)).timestamp()),
        "iss": ISSUER,
        "aud": AUDIENCE,
        "jti": str(uuid.uuid4()),
    })
    return jwt.encode(to_encode, PRIVATE_KEY, algorithm=JWT_ALGORITHM)


def decode_token(token: str) -> dict:
    try:
        payload = jwt.decode(
            token,
            PUBLIC_KEY,
            algorithms=[JWT_ALGORITHM],
            audience=AUDIENCE,
            issuer=ISSUER,
        )
        return payload
    except jwt.ExpiredSignatureError:
        raise ValueError("Token has expired")
    except jwt.JWTError as e:
        raise ValueError(f"Invalid token: {str(e)}")


def validate_token_claims(payload: dict) -> None:
    required_claims = ["sub", "department", "clearance_level", "role"]
    for claim in required_claims:
        if claim not in payload:
            raise ValueError(f"Missing required claim: {claim}")
