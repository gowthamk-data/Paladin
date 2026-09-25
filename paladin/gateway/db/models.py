import sqlalchemy as sa
from sqlalchemy import Column, Integer, String, Text, DateTime
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
