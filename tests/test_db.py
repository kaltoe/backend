from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import select
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import Base
from app.db.models import (
    Agenda,
    Meeting,
    OfftopicEvent,
    Participant,
    ShareLink,
    Utterance,
)

NOW = datetime(2026, 11, 3, 5, 0, tzinfo=UTC)


def meeting(**overrides: Any) -> Meeting:
    values: dict[str, Any] = {
        "id": "K7Q2MX",
        "title": "마케팅 전략 주간 회의",
        "planned_total_minutes": 30,
        "consent_agreed_at": NOW,
        "host_recovery_key_hash": "hash",
        "started_at": NOW,
        "expires_at": NOW + timedelta(days=14),
    }
    values.update(overrides)
    return Meeting(**values)


async def test_migration_matches_models(session: AsyncSession) -> None:
    """마이그레이션으로 만든 DB와 모델(ERD)이 다르면 autogenerate 가 차이를 낸다."""

    def diff(conn: Connection) -> list[Any]:
        return compare_metadata(MigrationContext.configure(conn), Base.metadata)

    conn = await session.connection()
    assert await conn.run_sync(diff) == []


async def test_meeting_defaults_and_relations(session: AsyncSession) -> None:
    session.add(meeting())
    await session.flush()
    agenda = Agenda(meeting_id="K7Q2MX", order_index=0, title="지난주 캠페인 성과 리뷰", allocated_minutes=10)
    host = Participant(meeting_id="K7Q2MX", name="진행자", role="HOST")
    session.add_all([agenda, host])
    await session.flush()
    session.add_all(
        [
            Utterance(meeting_id="K7Q2MX", agenda_id=agenda.id, source="REALTIME", text="안녕하세요", offset_ms=17000),
            OfftopicEvent(meeting_id="K7Q2MX", agenda_id=agenda.id, score=0.82, detected_at=NOW),
            ShareLink(meeting_id="K7Q2MX", share_id="q3Zx9LmPa1VbN7cK2dTg", created_by=host.id, expires_at=NOW),
        ]
    )
    await session.commit()

    m = await session.get(Meeting, "K7Q2MX")
    assert m is not None
    await session.refresh(m)
    assert (m.meeting_mode, m.status, m.paused_total_seconds, m.postprocess_status) == (
        "IN_PERSON",
        "IN_PROGRESS",
        0,
        "NONE",
    )
    assert m.recording_status is None and m.summary_status is None
    await session.refresh(agenda)
    assert agenda.keywords == [] and agenda.status == "PENDING"
    count = len((await session.scalars(select(Utterance).where(Utterance.meeting_id == "K7Q2MX"))).all())
    assert count == 1


@pytest.mark.parametrize(
    "overrides",
    [
        {"meeting_mode": "HYBRID"},
        {"target_total_minutes": 7},
        {"target_total_minutes": 185},
        {"planned_total_minutes": 0},
        {"attendee_count": 21},
        {"status": "PAUSED"},  # paused_at 없이 PAUSED
        {"paused_at": NOW},  # IN_PROGRESS 인데 paused_at 있음
        {"status": "ENDED", "ended_at": NOW},  # ended_by 없음
        {"status": "ENDED", "ended_at": NOW, "ended_by": "USER"},
        {"recording_status": "EXPIRED"},
        {"summary_status": "NONE"},
    ],
)
async def test_meeting_check_constraints(session: AsyncSession, overrides: dict[str, Any]) -> None:
    session.add(meeting(**overrides))
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_participant_name_unique_per_meeting(session: AsyncSession) -> None:
    session.add_all([meeting(), meeting(id="ABCDEF")])
    await session.flush()
    session.add_all(
        [
            Participant(meeting_id="K7Q2MX", name="민지", role="MEMBER"),
            Participant(meeting_id="ABCDEF", name="민지", role="MEMBER"),
        ]
    )
    await session.flush()  # 다른 회의면 같은 이름 가능

    session.add(Participant(meeting_id="K7Q2MX", name="민지", role="MEMBER"))
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_one_in_progress_agenda_per_meeting(session: AsyncSession) -> None:
    session.add(meeting())
    await session.flush()
    session.add_all(
        [
            Agenda(meeting_id="K7Q2MX", order_index=0, title="A", allocated_minutes=10, status="IN_PROGRESS"),
            Agenda(meeting_id="K7Q2MX", order_index=1, title="B", allocated_minutes=10, status="DONE"),
            Agenda(meeting_id="K7Q2MX", order_index=2, title="C", allocated_minutes=10, status="DONE"),
        ]
    )
    await session.flush()  # 끝난 안건은 여러 개여도 된다

    session.add(Agenda(meeting_id="K7Q2MX", order_index=3, title="D", allocated_minutes=10, status="IN_PROGRESS"))
    with pytest.raises(IntegrityError):
        await session.flush()
