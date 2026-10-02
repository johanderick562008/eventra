from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings

_settings = get_settings()

_pg_options = (
    f"-c statement_timeout={_settings.db_statement_timeout_ms} "
    f"-c lock_timeout={_settings.db_lock_timeout_ms} "
    "-c timezone=UTC"
)

engine = create_engine(
    _settings.database_url,
    pool_pre_ping=True,
    pool_size=_settings.db_pool_size,
    max_overflow=_settings.db_max_overflow,
    pool_timeout=_settings.db_pool_timeout_seconds,
    connect_args={"options": _pg_options},
)

# expire_on_commit=False lets services return ORM objects after committing without a re-query.
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    """One session per request. Services own commit; anything uncommitted is rolled back."""
    db = SessionLocal()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
