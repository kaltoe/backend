"""API-19 회의 시작. 만든 회의는 대기(WAITING)이고 [회의 시작]을 누른 시각부터 타이머가 흐른다."""

import asyncio
from datetime import timedelta
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import to_kst_iso
from app.db.models import Agenda, Meeting, Participant
from app.db.session import get_engine, get_sessionmaker
from app.main import create_app
from app.meetings import timer
from tests.test_agendas import T0, THREE_AGENDAS, at, bearer, error
from tests.test_realtime import ready, receive, room


async def waiting(client: httpx.AsyncClient) -> dict[str, Any]:
    """API-02 로 만든 그대로의 대기 회의."""
    return dict((await client.post("/api/v1/meetings", json=THREE_AGENDAS)).json()["data"])


async def start(client: httpx.AsyncClient, created: dict[str, Any], token: str | None = None) -> httpx.Response:
    headers = bearer(token or created["accessToken"])
    return await client.post(f"/api/v1/meetings/{created['meetingId']}/start", headers=headers)


async def state(client: httpx.AsyncClient, created: dict[str, Any]) -> dict[str, Any]:
    res = await client.get(f"/api/v1/meetings/{created['meetingId']}/state", headers=bearer(created["accessToken"]))
    return dict(res.json()["data"])


async def test_timer_counts_from_start_not_from_creation(
    client: httpx.AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    at(monkeypatch, 0)
    created = await waiting(client)
    first = created["agendas"][0]["agendaId"]

    at(monkeypatch, 300)  # 사람을 모으느라 5분 기다렸다
    res = await start(client, created)
    assert res.status_code == 200
    assert res.json()["data"] == {
        "startedAt": to_kst_iso(T0 + timedelta(seconds=300)),
        "timer": {
            "serverNow": to_kst_iso(T0 + timedelta(seconds=300)),
            "status": "IN_PROGRESS",
            "plannedTotalSeconds": 1800,
            "totalElapsedSeconds": 0,
            "pausedTotalSeconds": 0,
            "currentAgendaId": first,
            "agendaAllocatedSeconds": 600,
            "agendaElapsedSeconds": 0,
        },
    }
    meeting = await session.get(Meeting, created["meetingId"])
    assert meeting is not None
    await session.refresh(meeting)
    assert (meeting.status, meeting.started_at) == ("IN_PROGRESS", T0 + timedelta(seconds=300))
    agendas = (
        await session.scalars(select(Agenda).where(Agenda.meeting_id == meeting.id).order_by(Agenda.order_index))
    ).all()
    assert [(a.status, a.recording_start_ms) for a in agendas] == [
        ("IN_PROGRESS", 0),
        ("PENDING", None),
        ("PENDING", None),
    ]

    # 시작 31초 뒤. 대기 5분은 경과에 들어가지 않는다(API-04 는 같은 build_snapshot 을 쓴다)
    snapshot = timer.build_snapshot(meeting, agendas[0], T0 + timedelta(seconds=331))
    assert (snapshot.total_elapsed_seconds, snapshot.agenda_elapsed_seconds) == (31, 31)


async def test_host_only_and_auth(client: httpx.AsyncClient) -> None:
    created = await waiting(client)
    member = await client.post(f"/api/v1/meetings/{created['meetingId']}/sign-in", json={"name": "민지"})
    assert error(await start(client, created, member.json()["data"]["accessToken"])) == (403, "HOST_ONLY")
    no_token = await client.post(f"/api/v1/meetings/{created['meetingId']}/start")
    assert error(no_token) == (401, "UNAUTHORIZED")
    assert (await state(client, created))["status"] == "WAITING"  # 시작되지 않았다


async def test_only_waiting_meeting_starts(client: httpx.AsyncClient, session: AsyncSession) -> None:
    created = await waiting(client)
    assert (await start(client, created)).status_code == 200
    assert error(await start(client, created)) == (409, "INVALID_STATE")  # 다른 기기에서 먼저 누름

    await client.post(f"/api/v1/meetings/{created['meetingId']}/pause", headers=bearer(created["accessToken"]))
    assert error(await start(client, created)) == (409, "INVALID_STATE")  # 일시 정지에서 돌아가는 건 재개(API-06)

    await client.post(f"/api/v1/meetings/{created['meetingId']}/end", headers=bearer(created["accessToken"]))
    assert error(await start(client, created)) == (410, "MEETING_ENDED")


async def test_started_event_goes_to_everyone(app: FastAPI) -> None:
    async with room(app, start=False) as r:
        _, member_token = await r.member("민지")
        async with r.connect(r.host_token) as host:
            await ready(host)
            async with r.connect(member_token) as member:  # 대기 중에도 WS-01 에 붙는다
                await ready(member)
                await ready(host)  # participant.joined 를 비운다

                res = await r.client.post(f"/api/v1/meetings/{r.code}/start", headers=bearer(r.host_token))
                for ws in (host, member):
                    event = await receive(ws)
                    assert event["type"] == "meeting.started"
                    assert event["data"] == res.json()["data"]


async def test_two_devices_at_once_start_once(db_url: str) -> None:
    """같은 진행자 토큰으로 두 기기에서 동시에 눌러도 한 번만 시작된다. 실제 연결 풀로 커밋하고 끝나면 지운다."""
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()
    code = None
    try:
        app = create_app()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            created = (await client.post("/api/v1/meetings", json=THREE_AGENDAS)).json()["data"]
            code = created["meetingId"]
            results = await asyncio.gather(*(start(client, created) for _ in range(3)))
            assert sorted(r.status_code for r in results) == [200, 409, 409]
            async with get_sessionmaker()() as s:
                in_progress = await s.scalars(
                    select(Agenda).where(Agenda.meeting_id == code, Agenda.status == "IN_PROGRESS")
                )
                assert len(in_progress.all()) == 1
    finally:
        if code is not None:
            async with get_sessionmaker()() as s:
                for model in (Agenda, Participant, Meeting):
                    column = model.id if model is Meeting else model.meeting_id
                    await s.execute(delete(model).where(column == code))
                await s.commit()
        await get_engine().dispose()
        get_engine.cache_clear()
        get_sessionmaker.cache_clear()
