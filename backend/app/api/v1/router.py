from fastapi import APIRouter

from app.api.v1 import auth, bookings, events, organizer, users

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(auth.router)
api_router.include_router(users.router)
api_router.include_router(events.router)
api_router.include_router(bookings.router)
api_router.include_router(organizer.router)
