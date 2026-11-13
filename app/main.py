from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import health
from app.api.errors import install_error_handlers
from app.api.router import api_router
from app.config import get_settings
from app.log_filters import install_log_filters
from app.realtime import ws


def create_app() -> FastAPI:
    settings = get_settings()
    # 임시 키(app/api/auth.py)는 로컬 전용. 운영에서 빠뜨리면 재시작·워커마다 토큰이 무효가 되므로 뜨지 않는다
    if settings.app_env != "local" and not settings.jwt_secret:
        raise RuntimeError("JWT_SECRET 이 비어 있습니다. APP_ENV=local 이 아니면 반드시 설정해야 합니다.")
    install_log_filters()
    app = FastAPI(title="칼퇴 API", version="0.1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    install_error_handlers(app)
    app.include_router(health.router)
    app.include_router(api_router)
    app.include_router(ws.router)
    return app


app = create_app()
