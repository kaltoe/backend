"""DB 테스트 공용 픽스처.

DATABASE_URL 의 Postgres 에 마이그레이션을 적용하고, 테스트마다 트랜잭션을 롤백한다.
로컬에 DB가 없으면 DB 테스트만 건너뛴다. CI(CI=true)에서는 건너뛰지 않고 실패한다.
"""

import asyncio
import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import httpx
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.api.rate_limit import entry_limiter
from app.config import get_settings
from app.db.session import get_db
from app.main import create_app
from app.meetings import codes
from app.meetings.attempts import password_failures, recovery_key_failures
from app.meetings.idempotency import create_meeting_cache

ROOT = Path(__file__).resolve().parent.parent


def alembic_config(url: str) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


async def _reachable(url: str) -> bool:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
    finally:
        await engine.dispose()


@pytest.fixture(scope="session")
def db_url() -> str:
    url = get_settings().database_url
    if not asyncio.run(_reachable(url)):
        if os.environ.get("CI"):
            pytest.fail(f"CI 에서 DB에 연결하지 못했습니다: {url.split('@')[-1]}")
        pytest.skip("로컬 DB가 없어 DB 테스트를 건너뜁니다 (README 의 DB 항목 참고)")
    # 빈 DB에서 처음부터 적용해 downgrade/upgrade 가 모두 동작하는지 함께 확인한다
    cfg = alembic_config(url)
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    return url


@pytest.fixture
async def session(db_url: str) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(db_url)
    async with engine.connect() as conn:
        trans = await conn.begin()
        # 테스트 안의 commit 은 savepoint 에만 반영되고, 끝나면 바깥 트랜잭션째 롤백한다
        async with AsyncSession(bind=conn, join_transaction_mode="create_savepoint") as s:
            yield s
        await trans.rollback()
    await engine.dispose()


@pytest.fixture
async def client(session: AsyncSession) -> AsyncIterator[httpx.AsyncClient]:
    """실제 앱에 테스트 세션을 주입한 클라이언트. 요청의 커밋도 테스트가 끝나면 롤백된다."""
    app = create_app()

    async def override() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_db] = override
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture(autouse=True)
def _fast_bcrypt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(codes, "BCRYPT_ROUNDS", 4)


@pytest.fixture(autouse=True)
def _reset_memory_state() -> Iterator[None]:
    """요청 제한·실패 잠금·멱등성처럼 서버 메모리에 남는 상태를 테스트마다 비운다."""

    def reset() -> None:
        entry_limiter.reset()
        password_failures.reset()
        recovery_key_failures.reset()
        create_meeting_cache.clear()

    reset()
    yield
    reset()
