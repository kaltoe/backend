"""/api/v1 아래의 REST 라우터를 모은다. 기능 라우터는 여기에 include 한다."""

from fastapi import APIRouter

api_router = APIRouter(prefix="/api/v1")
