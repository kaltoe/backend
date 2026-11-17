"""데이터가 들어 있는 DB에서 마이그레이션을 오르내린다. 빈 DB는 conftest 의 db_url 이 매번 확인한다."""

import asyncio
from datetime import UTC, datetime
from typing import Any

from alembic import command
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from tests.conftest import alembic_config

BEFORE_WAITING = "4fc44ba82fe1"
MEETING = (
    "INSERT INTO meetings (id, title, planned_total_minutes, status, consent_agreed_at, host_recovery_key_hash, "
    "started_at, expires_at) VALUES (:id, '회의', 30, :status, '2026-11-03T05:00:00Z', 'hash', :started, "
    "'2026-11-17T05:00:00Z')"
)
AGENDA = (
    "INSERT INTO agendas (meeting_id, order_index, title, allocated_minutes, status, recording_start_ms) "
    "VALUES (:id, :order, '안건', 10, :status, :start)"
)

AGENDAS = "SELECT meeting_id, order_index, status, recording_start_ms FROM agendas ORDER BY meeting_id, order_index"


def run(url: str, *statements: tuple[str, dict[str, Any]]) -> list[Any]:
    """한 트랜잭션에서 차례로 실행하고 SELECT 결과만 순서대로 돌려준다."""

    async def go() -> list[Any]:
        engine = create_async_engine(url)
        rows = []
        try:
            async with engine.begin() as conn:
                for sql, params in statements:
                    result = await conn.execute(text(sql), params)
                    if sql.startswith("SELECT"):
                        rows.append(result.all())
            return rows
        finally:
            await engine.dispose()

    return asyncio.run(go())


def test_waiting_migration_keeps_existing_and_restores_waiting(db_url: str) -> None:
    cfg = alembic_config(db_url)
    try:
        command.downgrade(cfg, BEFORE_WAITING)
        started = datetime(2026, 11, 3, 5, 0, tzinfo=UTC)  # 동의 시각과 같게
        run(
            db_url,
            (MEETING, {"id": "OLD001", "status": "IN_PROGRESS", "started": started}),
            (AGENDA, {"id": "OLD001", "order": 0, "status": "IN_PROGRESS", "start": 0}),
        )
        command.upgrade(cfg, "head")  # 이미 시작된 회의는 그대로 통과

        run(
            db_url,
            (MEETING, {"id": "NEW001", "status": "WAITING", "started": None}),
            (AGENDA, {"id": "NEW001", "order": 0, "status": "PENDING", "start": None}),
            (AGENDA, {"id": "NEW001", "order": 1, "status": "PENDING", "start": None}),
        )
        command.downgrade(cfg, BEFORE_WAITING)  # 대기 회의는 만든 시각에 시작한 회의로 바뀐다

        meetings, agendas = run(
            db_url,
            ("SELECT id, status, started_at = consent_agreed_at FROM meetings ORDER BY id", {}),
            (AGENDAS, {}),
        )
        assert meetings == [("NEW001", "IN_PROGRESS", True), ("OLD001", "IN_PROGRESS", True)]
        assert agendas == [
            ("NEW001", 0, "IN_PROGRESS", 0),
            ("NEW001", 1, "PENDING", None),
            ("OLD001", 0, "IN_PROGRESS", 0),
        ]
    finally:
        run(db_url, ("DELETE FROM agendas", {}), ("DELETE FROM meetings", {}))
        command.upgrade(cfg, "head")
