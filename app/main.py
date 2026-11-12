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
