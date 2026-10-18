import sqlalchemy as sa
from sqlalchemy import Boolean, Column, Integer, String, Text, DateTime
from sqlalchemy.dialects.postgresql import UUID
import uuid
from datetime import datetime
from sqlalchemy.ext.declarative import declarative_base

Base = declarative_base()


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(80), unique=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    department = Column(String(80), nullable=False)
    clearance_level = Column(Integer, sa.CheckConstraint("clearance_level BETWEEN 1 AND 3"), nullable=False)
    role = Column(String(40), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class RequestLog(Base):
    __tablename__ = "request_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    request_id = Column(UUID(as_uuid=True), unique=True, nullable=False, default=uuid.uuid4)
    user_id = Column(Integer, sa.ForeignKey("users.id"), nullable=False)
    department = Column(String(80), nullable=False)
    clearance_level = Column(Integer, nullable=False)
    sanitized_prompt = Column(Text, nullable=False)
    llm_response = Column(Text)
    prompt_tokens = Column(Integer, default=0)
    completion_tokens = Column(Integer, default=0)
    status = Column(String(20), nullable=False, default="success")
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class DenialLog(Base):
    __tablename__ = "denial_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    request_id = Column(UUID(as_uuid=True), unique=True, nullable=False, default=uuid.uuid4)
    user_id = Column(Integer, sa.ForeignKey("users.id"), nullable=False)
    department = Column(String(80))
    clearance_level = Column(Integer)
    reason_code = Column(String(40), nullable=False)
    reason_detail = Column(Text)
    sanitized_prompt = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class GenerationRow(Base):
    """Authoritative pointer state for versioned Chroma generation collections.

    One row per generation collection managed by ``data/ingest.py``. At most
    one row may have ``is_active = true`` at any time, enforced by a partial
    unique index on the flag itself (created and migrated by
    ``gateway.rag.generations.GenerationStore.acquire``) — not merely by the
    single-transaction flip, which cannot protect against a manually edited
    or partially migrated table. ``activated_at`` is NULL until a generation
    has been active once; its ordering identifies the most recently deactivated
    generation, which is the rollback target. ``deactivated_at`` records when a
    generation lost the pointer: the in-use grace window for retention is
    measured from it, so a generation that served for weeks keeps its full
    grace period after cutover instead of being reclaimable immediately.
    """

    __tablename__ = "vector_generations"

    name = Column(String(255), primary_key=True)
    is_active = Column(Boolean, nullable=False, default=False, server_default="false")
    activated_at = Column(DateTime, nullable=True)
    # Set when a generation loses the pointer. Retention's in-use grace window
    # is measured from this timestamp, not from ``activated_at``, so a
    # long-serving generation is not reclaimable the moment it is superseded.
    # NULL for a generation that was never active, or is currently active.
    deactivated_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class PiiEvent(Base):
    __tablename__ = "pii_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    request_id = Column(UUID(as_uuid=True), nullable=False, default=uuid.uuid4)
    user_id = Column(Integer, sa.ForeignKey("users.id"), nullable=False)
    pii_type = Column(String(40), nullable=False)
    action = Column(String(20), nullable=False, default="REDACTED")
    masked_value = Column(String(100), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class AuthFailureLog(Base):
    __tablename__ = "auth_failure_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    request_id = Column(UUID(as_uuid=True), nullable=False, default=uuid.uuid4)
    username_attempted = Column(String(80), nullable=False)
    failure_reason = Column(String(80), nullable=False)
    ip_address = Column(String(45))
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
