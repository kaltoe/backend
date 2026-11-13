import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import health
from app.config import get_settings
from app.realtime import ws


def strip_query(record: logging.LogRecord) -> bool:
    """접속 로그의 경로에서 쿼리 문자열을 지운다. WebSocket 토큰이 ?token= 으로 오기 때문 (WS-01)."""
    if isinstance(record.args, tuple):
        record.args = tuple(a.partition("?")[0] if isinstance(a, str) and a.startswith("/") else a for a in record.args)
    return True


# HTTP 접속 로그는 uvicorn.access, WebSocket 접속 로그는 uvicorn.error 로 찍힌다
for _name in ("uvicorn.access", "uvicorn.error"):
    logging.getLogger(_name).addFilter(strip_query)


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="칼퇴 API", version="0.1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(health.router)
    app.include_router(ws.router)
    return app


app = create_app()
