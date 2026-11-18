from datetime import timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import to_kst_iso
from app.db.models import Meeting, OfftopicEvent
from tests.conftest import started
from tests.test_agendas import T0, THREE_AGENDAS, at, bearer, create, error, go_next, start_at_t0


async def result(client: httpx.AsyncClient, created: dict[str, Any], token: str | None = None) -> httpx.Response:
    headers = bearer(token or created["accessToken"])
    return await client.get(f"/api/v1/meetings/{created['meetingId']}/result", headers=headers)


async def finished(
    client: httpx.AsyncClient,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    body: dict[str, Any] = THREE_AGENDAS,
) -> tuple[dict[str, Any], list[int]]:
    """리뷰(10분)를 710초, 예산(12분)을 560초 하고 종료. 액션(8분)은 하지 못했다. 진행자 + 참가자 2명."""
    created = (await started(client, body)).json()["data"]
    code = created["meetingId"]
    ids = [a["agendaId"] for a in created["agendas"]]
    await start_at_t0(session, code)
    for name in ("민지", "준호"):
        await client.post(f"/api/v1/meetings/{code}/sign-in", json={"name": name})
    at(monkeypatch, 710)
    await go_next(client, created, ids[0])
    at(monkeypatch, 1270)
    await client.post(f"/api/v1/meetings/{code}/end", headers=bearer(created["accessToken"]))
    return created, ids


async def warn(session: AsyncSession, code: str, agenda_id: int, dismissed: bool = False) -> None:
    session.add(
        OfftopicEvent(
            meeting_id=code,
            agenda_id=agenda_id,
            score=0.8,
            detected_at=T0,
            dismissed_at=T0 if dismissed else None,
        )
    )
    await session.flush()


async def test_result_has_spec_shape(
    client: httpx.AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    created, (review, budget, action) = await finished(client, session, monkeypatch)
    code = created["meetingId"]
    await warn(session, code, review)
    await warn(session, code, review, dismissed=True)  # 닫힌 경고는 세지 않는다
    await warn(session, code, action)  # 끝내지 못한 안건은 빠지므로 보이지 않는다

    res = await result(client, created)
    assert res.status_code == 200
    assert res.json()["data"] == {
        "meetingId": code,
        "title": "마케팅 전략 주간 회의",
        "participantCount": 3,  # 한 번이라도 들어온 사람
        "startedAt": to_kst_iso(T0),
        "endedAt": to_kst_iso(T0 + timedelta(seconds=1270)),
        "endedBy": "HOST",
        "time": {
            "plannedTotalSeconds": 1800,  # 전체 안건 합(끝내지 못한 안건 포함)
            "actualDurationSeconds": 1270,
            "pausedTotalSeconds": 0,
            "diffSeconds": -530,
            "outcome": "SAVED",
        },
        "agendas": [
            {
                "agendaId": review,
                "orderIndex": 0,
                "title": "리뷰",
                "allocatedSeconds": 600,
                "actualSeconds": 710,
                "diffSeconds": 110,
                "outcome": "OVERTIME",
                "offtopicCount": 1,
                "recordingStartMs": 0,
                "recordingEndMs": 710_000,
            },
            {
                "agendaId": budget,
                "orderIndex": 1,
                "title": "예산",
                "allocatedSeconds": 720,
                "actualSeconds": 560,
                "diffSeconds": -160,
                "outcome": "SAVED",
                "offtopicCount": 0,
                "recordingStartMs": 710_000,
                "recordingEndMs": 1_270_000,
            },
        ],
        "summaryStatus": "PROCESSING",
        "recordingStatus": "PROCESSING",
        "postprocessStatus": "PROCESSING",
    }


async def test_unknown_duration_empties_times(
    client: httpx.AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    created, _ = await finished(client, session, monkeypatch)
    meeting = await session.get(Meeting, created["meetingId"])
    assert meeting is not None
    await session.refresh(meeting)
    meeting.actual_duration_seconds = None  # 시계 오류 등으로 소요 시간을 계산하지 못함
    await session.flush()

    data = (await result(client, created)).json()["data"]
    assert data["time"] == {
        "plannedTotalSeconds": 1800,
        "actualDurationSeconds": None,
        "pausedTotalSeconds": None,
        "diffSeconds": None,
        "outcome": "UNKNOWN",
    }
    agenda = data["agendas"][0]
    assert (agenda["actualSeconds"], agenda["diffSeconds"], agenda["outcome"]) == (None, None, "UNKNOWN")
    assert (agenda["recordingStartMs"], agenda["recordingEndMs"]) == (0, 710_000)  # 다시 듣기 구간은 그대로


async def test_on_time_within_60_seconds_and_remote(
    client: httpx.AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    created = (
        await started(
            client,
            THREE_AGENDAS
            | {"meetingMode": "REMOTE", "agendas": [{"title": "리뷰", "keywords": [], "allocatedMinutes": 10}]},
        )
    ).json()["data"]
    await start_at_t0(session, created["meetingId"])
    at(monkeypatch, 660)  # +60초는 ON_TIME
    await client.post(f"/api/v1/meetings/{created['meetingId']}/end", headers=bearer(created["accessToken"]))
    data = (await result(client, created)).json()["data"]
    assert (data["time"]["diffSeconds"], data["time"]["outcome"]) == (60, "ON_TIME")
    assert data["agendas"][0]["outcome"] == "ON_TIME"
    assert data["postprocessStatus"] == "NONE"  # 비대면은 CLOVA 후처리를 하지 않는다


async def test_not_ended_and_auth(client: httpx.AsyncClient) -> None:
    created = await create(client)
    assert error(await result(client, created)) == (409, "INVALID_STATE")  # 아직 진행 중
    assert error(await client.get(f"/api/v1/meetings/{created['meetingId']}/result")) == (401, "UNAUTHORIZED")
    other = await create(client)
    assert error(await result(client, created, other["accessToken"])) == (401, "UNAUTHORIZED")
