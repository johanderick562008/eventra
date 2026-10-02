import logging

from app.core.config import get_settings


def configure_logging() -> None:
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    # SQL echo would leak bound parameters into logs; keep SQLAlchemy quiet.
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
