"""/api/v1 아래의 REST 라우터를 모은다. 기능 라우터는 여기에 include 한다."""

from fastapi import APIRouter

from app.meetings import create, entry

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(create.router)
api_router.include_router(entry.router)
