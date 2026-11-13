from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import timedelta

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Meeting
from app.db.session import SessionFactory
from app.meetings import auto_end, timer
from tests.test_agendas import T0, bearer, create, start_at_t0
from tests.test_realtime import ready, receive, room

NOW = timer.utcnow()


@pytest.fixture(autouse=True)
def _forget_empty_rooms() -> Iterator[None]:
    auto_end.empty_since.clear()
    yield
    auto_end.empty_since.clear()


@pytest.fixture
def sessions(session: AsyncSession) -> SessionFactory:
    @asynccontextmanager
    async def borrowed() -> AsyncIterator[AsyncSession]:
        yield session  # 테스트 세션은 닫지 않는다

    return borrowed


async def reload(session: AsyncSession, code: str) -> Meeting:
    meeting = await session.get(Meeting, code)
    assert meeting is not None
    await session.refresh(meeting)
    return meeting


def seconds(n: float) -> timedelta:
    return timedelta(seconds=n)


async def test_ends_after_10_minutes_empty(
    client: httpx.AsyncClient, session: AsyncSession, sessions: SessionFactory
) -> None:
    code = (await create(client))["meetingId"]
    assert await auto_end.sweep(sessions, NOW) == []  # 이때부터 부재를 센다
    assert await auto_end.sweep(sessions, NOW + seconds(600)) == []  # 딱 10분은 아직
    assert await auto_end.sweep(sessions, NOW + seconds(601)) == [code]

    meeting = await reload(session, code)
    # 진행자 종료와 같은 상태·후처리, endedBy 만 AUTO
    assert (meeting.status, meeting.ended_by, meeting.ended_at) == ("ENDED", "AUTO", NOW + seconds(601))
    assert (meeting.recording_status, meeting.summary_status, meeting.postprocess_status) == (
        "PROCESSING",
        "PROCESSING",
        "PROCESSING",
    )
    # 이미 끝난 회의는 다시 끝내지 않는다
    assert await auto_end.sweep(sessions, NOW + seconds(1200)) == []
    assert code not in auto_end.empty_since


async def test_connection_resets_absence(app: FastAPI, session: AsyncSession, sessions: SessionFactory) -> None:
    async with room(app) as r:
        assert await auto_end.sweep(sessions, NOW) == []  # 아무도 없음 → 부재 시작
        async with r.connect(r.host_token) as host:
            await ready(host)
            assert await auto_end.sweep(sessions, NOW + seconds(601)) == []  # 접속 중이면 끝내지 않고 부재를 지운다
            assert r.code not in auto_end.empty_since
        assert await auto_end.sweep(sessions, NOW + seconds(700)) == []  # 다시 비었으니 여기서부터 센다
        assert await auto_end.sweep(sessions, NOW + seconds(1300)) == []
        assert await auto_end.sweep(sessions, NOW + seconds(1301)) == [r.code]


async def test_ends_60_minutes_past_target_even_if_paused(
    client: httpx.AsyncClient, session: AsyncSession, sessions: SessionFactory
) -> None:
    created = await create(client)  # 목표 30분
    code = created["meetingId"]
    await start_at_t0(session, code)
    await client.post(f"/api/v1/meetings/{code}/pause", headers=bearer(created["accessToken"]))
    deadline = T0 + timedelta(minutes=30 + 60)  # 일시 정지 시간도 60분에 포함한다(벽시계)

    assert await auto_end.sweep(sessions, deadline - seconds(1)) == []
    assert await auto_end.sweep(sessions, deadline) == [code]
    meeting = await reload(session, code)
    assert (meeting.status, meeting.ended_by, meeting.paused_at) == ("ENDED", "AUTO", None)


async def test_meeting_ended_event_says_auto(app: FastAPI, session: AsyncSession, sessions: SessionFactory) -> None:
    async with room(app) as r:
        await start_at_t0(session, r.code)  # 목표 10분 → T0 + 70분에 끝난다
        async with r.connect(r.host_token) as host:
            await ready(host)
            assert await auto_end.sweep(sessions, T0 + timedelta(minutes=70)) == [r.code]
            event = await receive(host)
            assert event["type"] == "meeting.ended"
            assert event["data"]["endedBy"] == "AUTO"
