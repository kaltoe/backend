from datetime import timedelta
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import to_kst_iso
from app.db.models import Agenda, Meeting
from app.meetings.end import actual_duration_seconds
from tests.test_agendas import T0, THREE_AGENDAS, at, bearer, create, error, start_at_t0
from tests.test_realtime import ready, receive, room


async def end(client: httpx.AsyncClient, created: dict[str, Any], token: str | None = None) -> httpx.Response:
    headers = bearer(token or created["accessToken"])
    return await client.post(f"/api/v1/meetings/{created['meetingId']}/end", headers=headers)


async def reload(session: AsyncSession, code: str) -> tuple[Meeting, list[Agenda]]:
    meeting = await session.get(Meeting, code)
    assert meeting is not None
    await session.refresh(meeting)  # API 가 커밋해 만료된 속성을 다시 읽는다
    agendas = list(await session.scalars(select(Agenda).where(Agenda.meeting_id == code).order_by(Agenda.order_index)))
    for agenda in agendas:
        await session.refresh(agenda)
    return meeting, agendas


async def test_end_running_meeting(
    client: httpx.AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    created = await create(client)
    code = created["meetingId"]
    await start_at_t0(session, code)
    at(monkeypatch, 1600.6)

    res = await end(client, created)
    assert res.status_code == 200
    assert res.json()["data"] == {
        "meetingId": code,
        "status": "ENDED",
        "endedAt": to_kst_iso(T0 + timedelta(seconds=1600.6)),
        "endedBy": "HOST",
        "actualDurationSeconds": 1600,
    }

    meeting, agendas = await reload(session, code)
    assert (meeting.status, meeting.ended_by, meeting.paused_at) == ("ENDED", "HOST", None)
    assert meeting.actual_duration_seconds == 1600
    assert (meeting.recording_status, meeting.summary_status, meeting.postprocess_status) == (
        "PROCESSING",
        "PROCESSING",
        "PROCESSING",
    )
    # 지금 안건만 닫히고, 끝내지 못한 안건은 PENDING 그대로
    assert [(a.status, a.recording_end_ms) for a in agendas] == [
        ("DONE", 1_600_600),
        ("PENDING", None),
        ("PENDING", None),
    ]


async def test_end_paused_meeting(
    client: httpx.AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    created = await create(client)
    code = created["meetingId"]
    await start_at_t0(session, code)
    at(monkeypatch, 100)
    await client.post(f"/api/v1/meetings/{code}/pause", headers=bearer(created["accessToken"]))
    at(monkeypatch, 160)

    res = await end(client, created)
    assert res.json()["data"]["actualDurationSeconds"] == 100

    meeting, agendas = await reload(session, code)
    assert (meeting.paused_at, meeting.paused_total_seconds) == (None, 60)
    assert agendas[0].recording_end_ms == 100_000  # 정지 시간은 녹음 위치에 들어가지 않는다


async def test_remote_meeting_skips_postprocess(client: httpx.AsyncClient, session: AsyncSession) -> None:
    created = (await client.post("/api/v1/meetings", json=THREE_AGENDAS | {"meetingMode": "REMOTE"})).json()["data"]
    assert (await end(client, created)).status_code == 200
    meeting, _ = await reload(session, created["meetingId"])
    assert (meeting.recording_status, meeting.summary_status, meeting.postprocess_status) == (
        "PROCESSING",
        "PROCESSING",
        "NONE",
    )


@pytest.mark.parametrize(
    ("started_after_t0", "paused_total"),
    [
        (0, 99_999),  # 정지 합계가 지난 시간보다 커 0 이하
        (7200, 0),  # 시작 시각이 종료보다 늦음(시계 오류)
    ],
)
async def test_bad_duration_is_null_but_ends(
    client: httpx.AsyncClient,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    started_after_t0: int,
    paused_total: int,
) -> None:
    created = await create(client)
    meeting = await start_at_t0(session, created["meetingId"])
    meeting.started_at = T0 + timedelta(seconds=started_after_t0)
    meeting.paused_total_seconds = paused_total
    await session.flush()
    at(monkeypatch, 600)

    res = await end(client, created)
    assert res.status_code == 200
    assert res.json()["data"]["actualDurationSeconds"] is None
    meeting, _ = await reload(session, created["meetingId"])
    assert (meeting.status, meeting.actual_duration_seconds) == ("ENDED", None)


async def test_already_ended_is_410(client: httpx.AsyncClient) -> None:
    created = await create(client)
    assert (await end(client, created)).status_code == 200
    assert error(await end(client, created)) == (410, "MEETING_ENDED")


async def test_host_only(client: httpx.AsyncClient) -> None:
    created = await create(client)
    code = created["meetingId"]
    member = (await client.post(f"/api/v1/meetings/{code}/sign-in", json={"name": "민지"})).json()["data"]
    assert error(await end(client, created, member["accessToken"])) == (403, "HOST_ONLY")
    assert error(await client.post(f"/api/v1/meetings/{code}/end")) == (401, "UNAUTHORIZED")


async def test_meeting_ended_goes_to_everyone(app: FastAPI) -> None:
    async with room(app) as r:
        _, member_token = await r.member("민지")
        async with r.connect(r.host_token) as host:
            await ready(host)
            async with r.connect(member_token) as member:
                await ready(member)
                await ready(host)  # participant.joined 를 비운다
                res = await r.client.post(f"/api/v1/meetings/{r.code}/end", headers=bearer(r.host_token))
                data = res.json()["data"]
                for ws in (host, member):
                    event = await receive(ws)
                    assert event["type"] == "meeting.ended"
                    assert event["data"] == {
                        "endedAt": data["endedAt"],
                        "endedBy": "HOST",
                        "actualDurationSeconds": data["actualDurationSeconds"],
                    }


def test_actual_duration_seconds() -> None:
    meeting = Meeting(started_at=T0, ended_at=T0 + timedelta(seconds=90), paused_total_seconds=30)
    assert actual_duration_seconds(meeting) == 60
    # DB 제약으로 저장되지는 않지만 함수는 실제 지난 시간보다 큰 값도 거른다
    meeting.paused_total_seconds = -10
    assert actual_duration_seconds(meeting) is None
    meeting.paused_total_seconds, meeting.paused_at = 30, T0 + timedelta(seconds=80)  # 일시 정지를 정리하지 못함
    assert actual_duration_seconds(meeting) is None
