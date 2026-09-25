from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker, Session
from gateway.config import DATABASE_URL
from gateway.db.models import Base
from gateway.errors import ServiceUnavailableError


engine = create_engine(DATABASE_URL, echo=False, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


def get_db() -> Session:
    # The engine connects lazily, so a dead database surfaces as an error while the endpoint
    # runs. Both paths end at the same place: a 503 with a generic detail and no driver text.
    try:
        db = SessionLocal()
    except SQLAlchemyError as exc:
        raise ServiceUnavailableError("database") from exc
    try:
        yield db
    finally:
        db.close()


def create_tables():
    Base.metadata.create_all(bind=engine)


def init_db():
    create_tables()
    return SessionLocal()
