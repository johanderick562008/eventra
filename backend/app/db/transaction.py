"""Explicit transaction boundaries with retry on deadlock / serialization failure."""

import logging
import random
import time
from collections.abc import Callable
from typing import TypeVar

from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.core.exceptions import RETRYABLE_SQLSTATES

logger = logging.getLogger(__name__)

T = TypeVar("T")


def run_in_transaction(db: Session, operation: Callable[[], T], *, attempts: int = 3) -> T:
    """Run ``operation`` and commit. Roll back on any failure.

    ``operation`` must (re)load every row it depends on, because a retry starts a fresh
    transaction. Business errors (``AppError``) are raised immediately, never retried.
    """
    for attempt in range(1, attempts + 1):
        try:
            result = operation()
            db.commit()
            return result
        except DBAPIError as exc:
            db.rollback()
            sqlstate = getattr(exc.orig, "sqlstate", None)
            if sqlstate in RETRYABLE_SQLSTATES and attempt < attempts:
                logger.warning(
                    "Retrying transaction after sqlstate=%s (attempt %d)", sqlstate, attempt
                )
                time.sleep(random.uniform(0.01, 0.05) * attempt)  # noqa: S311 (jitter)
                continue
            raise
        except BaseException:
            db.rollback()
            raise
    raise RuntimeError("unreachable")  # pragma: no cover
