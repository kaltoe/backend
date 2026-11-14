from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager
from functools import lru_cache

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings


@lru_cache
def get_engine() -> AsyncEngine:
    # 연결은 첫 쿼리 때 맺는다. DB 없이도 앱(/health)은 뜬다
    return create_async_engine(get_settings().database_url, pool_pre_ping=True)


@lru_cache
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(get_engine(), expire_on_commit=False)


async def get_db() -> AsyncIterator[AsyncSession]:
    """요청 하나에 세션 하나. 커밋은 각 API가 직접 한다."""
    async with get_sessionmaker()() as session:
        yield session


# 오래 열려 있는 곳(WebSocket, 주기 작업)은 세션을 계속 잡지 않고 필요할 때만 짧게 연다
SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]


def get_session_factory() -> SessionFactory:
    return get_sessionmaker()
