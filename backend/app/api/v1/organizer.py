from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.dependencies import CurrentOrganizer, DbSession
from app.schemas.common import PaginatedResponse, Pagination, error_responses, pagination_params
from app.schemas.event import OrganizerEventRead
from app.services import analytics_service

router = APIRouter(prefix="/organizer", tags=["Organizer"])


@router.get(
    "/events",
    response_model=PaginatedResponse[OrganizerEventRead],
    responses=error_responses(401, 403, 422),
    summary="My events with sales counters (organizer)",
)
def my_events(
    organizer: CurrentOrganizer,
    db: DbSession,
    pagination: Annotated[Pagination, Depends(pagination_params)],
) -> PaginatedResponse[OrganizerEventRead]:
    """All of the caller's non-deleted events (including cancelled and past), newest start first."""
    items, total = analytics_service.organizer_events(db, organizer, pagination)
    return PaginatedResponse(data=items, meta=pagination.meta(total))
