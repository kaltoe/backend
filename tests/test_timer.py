from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import ApiError, ErrorCode
from app.db.models import Agenda, Meeting, MeetingStatus
from app.meetings.timer import (
    Outcome,
    build_snapshot,
    current_agenda,
    ensure_transition,
    judge,
    lock_meeting,
    timeline_ms,
)

START = datetime(2026, 11, 3, 5, 0, tzinfo=UTC)  # 14:00 KST


def at(seconds: float) -> datetime:
    return START + timedelta(seconds=seconds)


def meeting(**overrides: Any) -> Meeting:
    values: dict[str, Any] = {
        "id": "K7Q2MX",
        "title": "마케팅 전략 주간 회의",
        "status": "IN_PROGRESS",
        "planned_total_minutes": 30,
        "paused_total_seconds": 0,
        "consent_agreed_at": START,
        "host_recovery_key_hash": "hash",
        "started_at": START,
        "expires_at": START + timedelta(days=14),
    }
    values.update(overrides)
    return Meeting(**values)


def agenda(**overrides: Any) -> Agenda:
    values: dict[str, Any] = {
        "id": 12,
        "meeting_id": "K7Q2MX",
        "order_index": 0,
        "title": "지난주 캠페인 성과 리뷰",
        "allocated_minutes": 10,
        "status": "IN_PROGRESS",
        "recording_start_ms": 0,
    }
    values.update(overrides)
    return Agenda(**values)


def test_snapshot_in_progress_matches_spec_example() -> None:
    snap = build_snapshot(meeting(), agenda(), at(31))
    assert snap.model_dump(mode="json", by_alias=True) == {
        "serverNow": "2026-11-03T14:00:31+09:00",
        "status": "IN_PROGRESS",
        "plannedTotalSeconds": 1800,
        "totalElapsedSeconds": 31,
        "pausedTotalSeconds": 0,
        "currentAgendaId": 12,
        "agendaAllocatedSeconds": 600,
        "agendaElapsedSeconds": 31,
    }


def test_paused_meeting_stops_the_clock() -> None:
    # 100초 진행 → 일시 정지 → 지금은 정지 후 50초
    m = meeting(status="PAUSED", paused_at=at(100))
    assert timeline_ms(m, at(150)) == 100_000
    assert build_snapshot(m, agenda(), at(150)).total_elapsed_seconds == 100


def test_resumed_meeting_subtracts_paused_total() -> None:
    # 100초 진행, 80초 정지 후 재개, 지금은 재개 후 20초
    m = meeting(paused_total_seconds=80)
    assert timeline_ms(m, at(200)) == 120_000


def test_second_agenda_elapsed_counts_from_its_start() -> None:
    second = agenda(id=13, order_index=1, allocated_minutes=12, recording_start_ms=710_000)
    snap = build_snapshot(meeting(), second, at(720))
    assert (snap.current_agenda_id, snap.agenda_allocated_seconds, snap.agenda_elapsed_seconds) == (13, 720, 10)


def test_ended_meeting_freezes_at_ended_at_and_has_no_agenda() -> None:
    m = meeting(status="ENDED", ended_at=at(1600), ended_by="HOST", paused_total_seconds=80)
    snap = build_snapshot(m, None, at(5000))
    assert snap.total_elapsed_seconds == 1520
    assert (snap.current_agenda_id, snap.agenda_allocated_seconds, snap.agenda_elapsed_seconds) == (None, 0, 0)


@pytest.mark.parametrize(
    ("diff", "expected"),
    [
        (-61, Outcome.SAVED),
        (-60, Outcome.ON_TIME),
        (0, Outcome.ON_TIME),
        (60, Outcome.ON_TIME),
        (61, Outcome.OVERTIME),
        (None, Outcome.UNKNOWN),
    ],
)
def test_judge_boundaries(diff: int | None, expected: Outcome) -> None:
    assert judge(diff) is expected


@pytest.mark.parametrize(
    ("current", "target"),
    [
        ("IN_PROGRESS", MeetingStatus.PAUSED),
        ("PAUSED", MeetingStatus.IN_PROGRESS),
        ("IN_PROGRESS", MeetingStatus.ENDED),
        ("PAUSED", MeetingStatus.ENDED),
    ],
)
def test_allowed_transitions(current: str, target: MeetingStatus) -> None:
    ensure_transition(current, target)


@pytest.mark.parametrize(
    ("current", "target", "code"),
    [
        ("PAUSED", MeetingStatus.PAUSED, ErrorCode.INVALID_STATE),
        ("IN_PROGRESS", MeetingStatus.IN_PROGRESS, ErrorCode.INVALID_STATE),
        ("ENDED", MeetingStatus.PAUSED, ErrorCode.MEETING_ENDED),
        ("ENDED", MeetingStatus.ENDED, ErrorCode.MEETING_ENDED),
    ],
)
def test_rejected_transitions(current: str, target: MeetingStatus, code: ErrorCode) -> None:
    with pytest.raises(ApiError) as exc:
        ensure_transition(current, target)
    assert exc.value.code is code


async def test_lock_meeting_and_current_agenda(session: AsyncSession) -> None:
    session.add(meeting())
    await session.flush()
    session.add_all(
        [
            Agenda(meeting_id="K7Q2MX", order_index=0, title="리뷰", allocated_minutes=10, status="DONE"),
            Agenda(meeting_id="K7Q2MX", order_index=1, title="예산", allocated_minutes=12, status="IN_PROGRESS"),
            Agenda(meeting_id="K7Q2MX", order_index=2, title="액션", allocated_minutes=8),
        ]
    )
    await session.flush()

    locked = await lock_meeting(session, "K7Q2MX")
    assert locked.id == "K7Q2MX"
    current = await current_agenda(session, "K7Q2MX")
    assert current is not None and current.order_index == 1

    with pytest.raises(ApiError) as exc:
        await lock_meeting(session, "NOPE00")
    assert exc.value.code is ErrorCode.MEETING_NOT_FOUND
