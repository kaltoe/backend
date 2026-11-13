from fastapi import APIRouter

from app.api.docs import HEALTH

router = APIRouter(tags=[HEALTH])


@router.get("/health", summary="서버 상태 확인", description='서버가 떠 있으면 `{"status": "ok"}`입니다.')
def health() -> dict[str, str]:
    return {"status": "ok"}
