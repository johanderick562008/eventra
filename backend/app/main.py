import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.api.v1.router import api_router
from app.core.config import get_settings
from app.core.exceptions import register_exception_handlers
from app.core.logging import configure_logging
from app.db.session import engine
from app.middleware.request_context import RequestContextMiddleware
from app.tasks.event_status import run_event_status_sweeper

logger = logging.getLogger(__name__)

API_DESCRIPTION = """
REST API for an event ticketing platform with **attendees** and **organizers**.

### Conventions
* Authenticate with `Authorization: Bearer <access_token>` (from `POST /api/v1/auth/login`).
* Successful responses are wrapped: `{"data": ...}`.
  Lists add `"meta": {page, limit, total, pages}`.
* Errors always look like `{"detail": "...", "code": "MACHINE_CODE"}` (+ `errors` for 422).
* Timestamps are ISO 8601 with offset; money is a decimal string (`"499.00"`).

### Booking lifecycle
`CONFIRMED` -> `CANCELLED` (attendee cancels, tickets released) or
`CONFIRMED` -> `REFUND_PENDING` (organizer cancels the event). `REFUNDED` is reserved for a
future payment integration; no real money is moved by this API.

### Concurrency
Booking locks the event row (`SELECT ... FOR UPDATE`), re-checks capacity, inserts the booking and
increments `tickets_sold` in one transaction. A `CHECK (tickets_sold <= capacity)` constraint is a
second safeguard. Losing a race for the last ticket returns **409 `INSUFFICIENT_CAPACITY`**.
"""

TAGS = [
    {"name": "Auth", "description": "Registration, login and refresh-token rotation"},
    {"name": "Users", "description": "Self-service profile management"},
    {"name": "Events", "description": "Public discovery and organizer event management"},
    {"name": "Bookings", "description": "Ticket booking, history and cancellation"},
    {"name": "Organizer", "description": "Dashboard: own events, bookings and sales summaries"},
    {"name": "Health", "description": "Liveness and readiness probes"},
]


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    logger.info(
        "Starting %s v%s (env=%s)", settings.app_name, settings.app_version, settings.environment
    )
    stop = asyncio.Event()
    sweeper = None
    if settings.event_status_sweep_interval_seconds > 0:
        sweeper = asyncio.create_task(
            run_event_status_sweeper(settings.event_status_sweep_interval_seconds, stop)
        )
    yield
    stop.set()
    if sweeper is not None:
        with suppress(asyncio.CancelledError):
            await sweeper
    engine.dispose()
    logger.info("Shutdown complete")


def create_app() -> FastAPI:
    configure_logging()
    settings = get_settings()
    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        description=API_DESCRIPTION,
        openapi_tags=TAGS,
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.frontend_origins,
        allow_credentials=False,  # bearer tokens, not cookies
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
        expose_headers=["X-Request-ID", "Retry-After"],
    )
    app.add_middleware(RequestContextMiddleware, hsts=settings.is_production)
    register_exception_handlers(app)
    app.include_router(api_router)

    @app.get("/health", tags=["Health"], summary="Liveness probe")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ready", tags=["Health"], summary="Readiness probe (checks the database)")
    def ready() -> JSONResponse:
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
        except Exception:
            logger.exception("Readiness check failed")
            return JSONResponse(
                {"status": "unavailable", "database": "unavailable"}, status_code=503
            )
        return JSONResponse({"status": "ready", "database": "ok"})

    return app


app = create_app()
