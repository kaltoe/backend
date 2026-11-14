import asyncio
from datetime import timedelta
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Agenda, Meeting, Participant
from app.db.session import get_engine, get_sessionmaker
from app.main import create_app
from app.meetings import timer
from tests.test_realtime import nothing_arrives, ready, receive, room

T0 = timer.utcnow().replace(microsecond=0) - timedelta(hours=1)

THREE_AGENDAS = {
    "title": "마케팅 전략 주간 회의",
    "consentAgreed": True,
    "agendas": [
        {"title": "리뷰", "keywords": [], "allocatedMinutes": 10},
        {"title": "예산", "keywords": [], "allocatedMinutes": 12},
        {"title": "액션", "keywords": [], "allocatedMinutes": 8},
    ],
}


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def create(client: httpx.AsyncClient) -> dict[str, Any]:
    data: dict[str, Any] = (await client.post("/api/v1/meetings", json=THREE_AGENDAS)).json()["data"]
    return data


async def start_at_t0(session: AsyncSession, code: str) -> Meeting:
    meeting = await session.get(Meeting, code)
    assert meeting is not None
    meeting.started_at = T0
    await session.flush()
    return meeting


def at(monkeypatch: pytest.MonkeyPatch, seconds: float) -> None:
    monkeypatch.setattr(timer, "utcnow", lambda: T0 + timedelta(seconds=seconds))


async def go_next(client: httpx.AsyncClient, created: dict[str, Any], from_agenda_id: Any) -> httpx.Response:
    return await client.post(
        f"/api/v1/meetings/{created['meetingId']}/agendas/next",
        json={"fromAgendaId": from_agenda_id},
        headers=bearer(created["accessToken"]),
    )


def error(res: httpx.Response) -> tuple[int, str]:
    return res.status_code, res.json()["error"]["code"]


async def test_next_agenda(client: httpx.AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    created = await create(client)
    first, second, _ = (a["agendaId"] for a in created["agendas"])
    await start_at_t0(session, created["meetingId"])
    at(monkeypatch, 710.4)

    res = await go_next(client, created, first)
    assert res.status_code == 200
    assert res.json()["data"]["previousAgenda"] == {
        "agendaId": first,
        "actualSeconds": 710,
        "diffSeconds": 110,
        "outcome": "OVERTIME",
    }
    assert res.json()["data"]["currentAgenda"] == {
        "agendaId": second,
        "orderIndex": 1,
        "title": "예산",
        "allocatedSeconds": 720,
    }
    snapshot = res.json()["data"]["timer"]
    assert snapshot["status"] == "IN_PROGRESS"
    assert snapshot["totalElapsedSeconds"] == 710
    assert (snapshot["currentAgendaId"], snapshot["agendaAllocatedSeconds"], snapshot["agendaElapsedSeconds"]) == (
        second,
        720,
        0,
    )

    done, now_running = await session.get(Agenda, first), await session.get(Agenda, second)
    assert done is not None and now_running is not None
    await session.refresh(done)
    await session.refresh(now_running)
    assert (done.status, done.recording_end_ms) == ("DONE", 710_400)
    assert (now_running.status, now_running.recording_start_ms) == ("IN_PROGRESS", 710_400)


@pytest.mark.parametrize(
    ("elapsed", "outcome"),
    [(539, "SAVED"), (540, "ON_TIME"), (660, "ON_TIME"), (661, "OVERTIME")],  # 배정 600초, ±60초 허용
)
async def test_outcome_follows_60_second_rule(
    client: httpx.AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch, elapsed: int, outcome: str
) -> None:
    created = await create(client)
    await start_at_t0(session, created["meetingId"])
    at(monkeypatch, elapsed)
    res = await go_next(client, created, created["agendas"][0]["agendaId"])
    assert res.json()["data"]["previousAgenda"]["outcome"] == outcome


async def test_stale_or_foreign_agenda_is_409(client: httpx.AsyncClient) -> None:
    created = await create(client)
    other = await create(client)
    first = created["agendas"][0]["agendaId"]
    assert (await go_next(client, created, first)).status_code == 200
    # 같은 요청을 다시 보내도 안건을 건너뛰지 않는다
    assert error(await go_next(client, created, first)) == (409, "AGENDA_CHANGED")
    assert error(await go_next(client, created, other["agendas"][0]["agendaId"])) == (409, "AGENDA_CHANGED")


async def test_last_agenda_is_409(client: httpx.AsyncClient) -> None:
    created = await create(client)
    first, second, third = (a["agendaId"] for a in created["agendas"])
    assert (await go_next(client, created, first)).status_code == 200
    assert (await go_next(client, created, second)).status_code == 200
    assert error(await go_next(client, created, third)) == (409, "LAST_AGENDA")


async def test_paused_meeting_resumes_with_one_event(
    app: FastAPI, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with room(app) as r:
        # room() 의 회의는 안건이 하나뿐이라 안건이 셋인 회의를 같은 클라이언트로 만든다
        created = await create(r.client)
        r.code, r.host_token = created["meetingId"], created["accessToken"]
        await start_at_t0(session, r.code)
        async with r.connect(r.host_token) as host:
            await ready(host)
            at(monkeypatch, 100)
            await r.client.post(f"/api/v1/meetings/{r.code}/pause", headers=bearer(r.host_token))
            assert (await receive(host))["type"] == "meeting.paused"

            at(monkeypatch, 160)
            res = await go_next(r.client, created, created["agendas"][0]["agendaId"])
            assert res.status_code == 200
            snapshot = res.json()["data"]["timer"]
            assert snapshot["status"] == "IN_PROGRESS"
            assert snapshot["pausedTotalSeconds"] == 60
            assert res.json()["data"]["previousAgenda"]["actualSeconds"] == 100  # 정지 시간은 빠진다
            event = await receive(host)
            assert event["type"] == "agenda.changed"
            assert event["data"] == res.json()["data"]
            assert await nothing_arrives(host)  # meeting.resumed 는 따로 보내지 않는다


async def test_ended_meeting_is_410(client: httpx.AsyncClient, session: AsyncSession) -> None:
    created = await create(client)
    meeting = await start_at_t0(session, created["meetingId"])
    meeting.status, meeting.ended_at, meeting.ended_by = "ENDED", timer.utcnow(), "HOST"
    await session.flush()
    assert error(await go_next(client, created, created["agendas"][0]["agendaId"])) == (410, "MEETING_ENDED")


async def test_host_only_and_validation(client: httpx.AsyncClient) -> None:
    created = await create(client)
    code, first = created["meetingId"], created["agendas"][0]["agendaId"]
    member = (await client.post(f"/api/v1/meetings/{code}/sign-in", json={"name": "민지"})).json()["data"]
    res = await client.post(
        f"/api/v1/meetings/{code}/agendas/next", json={"fromAgendaId": first}, headers=bearer(member["accessToken"])
    )
    assert error(res) == (403, "HOST_ONLY")
    res = await client.post(f"/api/v1/meetings/{code}/agendas/next", json={"fromAgendaId": first})
    assert error(res) == (401, "UNAUTHORIZED")
    for bad in ("12", None):
        assert error(await go_next(client, created, bad)) == (400, "VALIDATION_ERROR")


async def test_concurrent_requests_move_only_once(db_url: str) -> None:
    """같은 fromAgendaId 요청이 동시에 와도 회의 행 잠금 때문에 하나만 넘어간다.

    다른 테스트는 세션 하나를 공유해 잠금을 볼 수 없으므로, 여기서는 실제 연결 풀로 커밋하고 끝나면 지운다.
    """
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()
    code = None
    try:
        app = create_app()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            created = await create(client)
            code = created["meetingId"]
            first, second, _ = (a["agendaId"] for a in created["agendas"])
            results = await asyncio.gather(*(go_next(client, created, first) for _ in range(5)))
            assert sorted(r.status_code for r in results) == [200, 409, 409, 409, 409]

            state = (await client.get(f"/api/v1/meetings/{code}/state", headers=bearer(created["accessToken"]))).json()
            assert [a["status"] for a in state["data"]["agendas"]] == ["DONE", "IN_PROGRESS", "PENDING"]
            assert state["data"]["timer"]["currentAgendaId"] == second
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
