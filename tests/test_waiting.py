"""대기(WAITING) 중 입장·상태 조회(#66). 대기 화면(진행자·참가자)을 그릴 값이 노션 API-01·03·04 비고와 같아야 한다."""

from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from app.realtime import ws as ws_module
from tests.test_agendas import THREE_AGENDAS, bearer
from tests.test_realtime import nothing_arrives, ready, receive, room


async def waiting(client: httpx.AsyncClient, **overrides: Any) -> dict[str, Any]:
    return dict((await client.post("/api/v1/meetings", json=THREE_AGENDAS | overrides)).json()["data"])


async def sign_in(client: httpx.AsyncClient, code: str, name: str) -> dict[str, Any]:
    return dict((await client.post(f"/api/v1/meetings/{code}/sign-in", json={"name": name})).json()["data"])


async def state(client: httpx.AsyncClient, code: str, token: str) -> dict[str, Any]:
    return dict((await client.get(f"/api/v1/meetings/{code}/state", headers=bearer(token))).json()["data"])


async def test_entry_and_sign_in_while_waiting(client: httpx.AsyncClient) -> None:
    created = await waiting(client)
    code = created["meetingId"]

    entry = await client.get(f"/api/v1/meetings/{code}/entry")
    assert entry.status_code == 200
    assert entry.json()["data"]["status"] == "WAITING"

    member = await sign_in(client, code, "민지")
    assert member["meetingStatus"] == "WAITING"  # 참가자 대기 화면으로

    await client.post(f"/api/v1/meetings/{code}/start", headers=bearer(created["accessToken"]))
    late = await sign_in(client, code, "준호")
    assert late["meetingStatus"] == "IN_PROGRESS"  # 늦게 온 사람은 대기 없이 바로 합류


async def test_state_while_waiting_matches_spec(client: httpx.AsyncClient) -> None:
    created = await waiting(client, attendeeCount=5)
    code = created["meetingId"]
    member = await sign_in(client, code, "민지")
    first = created["agendas"][0]["agendaId"]

    data = await state(client, code, member["accessToken"])
    assert (data["status"], data["connectedCount"], data["attendeeCount"]) == ("WAITING", 0, 5)
    assert {k: v for k, v in data["timer"].items() if k != "serverNow"} == {
        "status": "WAITING",
        "plannedTotalSeconds": 1800,
        "totalElapsedSeconds": 0,
        "pausedTotalSeconds": 0,
        "currentAgendaId": first,  # 시작하면 진행될 첫 안건
        "agendaAllocatedSeconds": 600,
        "agendaElapsedSeconds": 0,
    }
    assert [(a["status"], a["actualSeconds"]) for a in data["agendas"]] == [("PENDING", None)] * 3
    assert [(p["name"], p["isConnected"]) for p in data["participants"]] == [("진행자", False), ("민지", False)]
    assert data["transcripts"] == []
    assert (data["sttStatus"], data["offtopicStatus"], data["activeOfftopicEvent"]) == ("OK", "OK", None)
    assert data["recording"] == {"status": "PAUSED", "sourceParticipantId": None}


async def test_attendee_count_null_when_not_given(client: httpx.AsyncClient) -> None:
    created = await waiting(client)
    assert (await state(client, created["meetingId"], created["accessToken"]))["attendeeCount"] is None


async def test_ws_works_while_waiting_without_timer_sync(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ws_module, "TIMER_SYNC_SECONDS", 0.1)
    async with room(app, start=False) as r:
        member_id, member_token = await r.member("민지")
        async with r.connect(r.host_token) as host:
            await ready(host)
            async with r.connect(member_token) as member:
                await ready(member)
                joined = await receive(host)  # 대기 화면 입장 현황이 바로 바뀐다
                assert (joined["type"], joined["data"]["participantId"]) == ("participant.joined", member_id)
                data = await state(r.client, r.code, r.host_token)
                assert data["connectedCount"] == 2
                assert await nothing_arrives(host, wait=0.5)  # 대기 중에는 timer.sync 가 없다

            # 나간 것도 바로 바뀐다. 끊김 처리와 ping 응답 중 무엇이 먼저 올지 몰라 left 가 올 때까지 읽는다
            while (message := await receive(host))["type"] != "participant.left":
                assert message["type"] == "pong" or message["type"] == "participant.updated"
            assert message["data"]["participantId"] == member_id
