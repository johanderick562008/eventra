from datetime import UTC, datetime


def utcnow() -> datetime:
    """Timezone-aware current time. Never use naive datetimes in this code base."""
    return datetime.now(UTC)
