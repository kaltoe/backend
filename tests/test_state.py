from typing import Any

import httpx
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import create_access_token
from app.db.models import Agenda, Meeting, Utterance
from app.meetings.timer import utcnow
from tests.test_realtime import ready, room

TWO_AGENDAS = {
    "title": "마케팅 전략 주간 회의",
    "consentAgreed": True,
    "agendas": [
        {"title": "리뷰", "keywords": ["CTR"], "allocatedMinutes": 10},
        {"title": "예산", "keywords": [], "allocatedMinutes": 12},
    ],
}

FIELDS = {
    "meetingId",
    "title",
    "meetingMode",
    "status",
    "connectedCount",
    "invite",
    "timer",
    "agendas",
    "me",
    "participants",
    "transcripts",
    "sttStatus",
    "recording",
    "offtopicStatus",
    "activeOfftopicEvent",
}


async def create(client: httpx.AsyncClient, **overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = (await client.post("/api/v1/meetings", json=TWO_AGENDAS | overrides)).json()["data"]
    return data


async def get_state(client: httpx.AsyncClient, code: str, token: str | None) -> httpx.Response:
    headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
    return await client.get(f"/api/v1/meetings/{code}/state", headers=headers)


async def test_state_of_new_meeting(client: httpx.AsyncClient) -> None:
    created = await create(client)
    res = await get_state(client, created["meetingId"], created["accessToken"])
    assert res.status_code == 200
    data = res.json()["data"]
    assert set(data) == FIELDS
    assert data["meetingMode"] == "IN_PERSON"
    assert data["status"] == "IN_PROGRESS"
    assert data["connectedCount"] == 0
    assert data["invite"] == created["invite"]
    assert data["timer"]["currentAgendaId"] == created["agendas"][0]["agendaId"]
    assert [a["status"] for a in data["agendas"]] == ["IN_PROGRESS", "PENDING"]
    assert [a["actualSeconds"] for a in data["agendas"]] == [None, None]
    assert data["agendas"][0]["allocatedSeconds"] == 600
    host_id = created["host"]["participantId"]
    assert data["me"] == {"participantId": host_id, "name": "진행자", "role": "HOST", "micState": "ON"}
    assert data["participants"] == [
        {"participantId": host_id, "name": "진행자", "role": "HOST", "isConnected": False, "micState": "ON"}
    ]
    assert data["transcripts"] == []
    assert data["sttStatus"] == "OK"
    assert data["recording"] == {"status": "RECORDING", "sourceParticipantId": None}
    assert data["offtopicStatus"] == "OK"
    assert data["activeOfftopicEvent"] is None


async def test_transcripts_are_current_agenda_latest_200_oldest_first(
    client: httpx.AsyncClient, session: AsyncSession
) -> None:
    created = await create(client)
    code = created["meetingId"]
    current, other = (a["agendaId"] for a in created["agendas"])
    session.add_all(
        [
            Utterance(meeting_id=code, agenda_id=current, source="REALTIME", text=f"말 {i}", offset_ms=i * 1000)
            for i in range(201)
        ]
        + [
            Utterance(meeting_id=code, agenda_id=other, source="REALTIME", text="다른 안건", offset_ms=500),
            Utterance(meeting_id=code, agenda_id=current, source="POSTPROCESS", text="후처리", offset_ms=600),
        ]
    )
    await session.flush()

    transcripts = (await get_state(client, code, created["accessToken"])).json()["data"]["transcripts"]
    assert len(transcripts) == 200
    assert [t["offsetMs"] for t in transcripts] == [i * 1000 for i in range(1, 201)]  # 가장 오래된 0번이 빠진다
    assert {t["agendaId"] for t in transcripts} == {current}
    assert transcripts[0]["participantId"] is None and transcripts[0]["speakerName"] is None  # 대면


async def test_remote_transcripts_have_speaker_name(client: httpx.AsyncClient, session: AsyncSession) -> None:
    created = await create(client, meetingMode="REMOTE")
    code = created["meetingId"]
    member = (await client.post(f"/api/v1/meetings/{code}/sign-in", json={"name": "민지"})).json()["data"]
    session.add(
        Utterance(
            meeting_id=code,
            agenda_id=created["agendas"][0]["agendaId"],
            participant_id=member["participantId"],
            source="REALTIME",
            speaker_label="민지",
            text="안녕하세요",
            offset_ms=1000,
        )
    )
    await session.flush()

    data = (await get_state(client, code, member["accessToken"])).json()["data"]
    assert data["meetingMode"] == "REMOTE"
    assert data["me"]["role"] == "MEMBER"
    assert data["transcripts"][0]["participantId"] == member["participantId"]
    assert data["transcripts"][0]["speakerName"] == "민지"


async def test_connection_and_mic_follow_websocket(app: FastAPI) -> None:
    async with room(app) as r:
        member_id, member_token = await r.member("민지")
        async with r.connect(member_token) as ws:
            await ready(ws)
            await ws.send_json({"type": "mic.status", "data": {"state": "OFF"}})
            await ready(ws)
            data = (await get_state(r.client, r.code, r.host_token)).json()["data"]
    assert data["connectedCount"] == 1
    by_id = {p["participantId"]: p for p in data["participants"]}
    assert by_id[member_id]["isConnected"] is True
    assert by_id[member_id]["micState"] == "OFF"
    assert by_id[r.host_id]["isConnected"] is False
    assert data["me"]["micState"] == "ON"


async def test_paused_meeting_reports_paused_recording(client: httpx.AsyncClient, session: AsyncSession) -> None:
    created = await create(client)
    meeting = await session.get(Meeting, created["meetingId"])
    assert meeting is not None
    meeting.status, meeting.paused_at = "PAUSED", utcnow()
    await session.flush()

    data = (await get_state(client, meeting.id, created["accessToken"])).json()["data"]
    assert data["status"] == "PAUSED"
    assert data["recording"] == {"status": "PAUSED", "sourceParticipantId": None}


async def test_ended_meeting_is_200_with_actual_seconds(client: httpx.AsyncClient, session: AsyncSession) -> None:
    created = await create(client)
    code = created["meetingId"]
    first, second = [await session.get(Agenda, a["agendaId"]) for a in created["agendas"]]
    meeting = await session.get(Meeting, code)
    assert meeting is not None and first is not None and second is not None
    first.status, first.recording_end_ms = "DONE", 90_500
    second.status, second.recording_start_ms, second.recording_end_ms = "DONE", 90_500, 150_000
    meeting.status, meeting.ended_at, meeting.ended_by = "ENDED", utcnow(), "HOST"
    session.add(Utterance(meeting_id=code, agenda_id=second.id, source="REALTIME", text="끝", offset_ms=100_000))
    await session.flush()

    res = await get_state(client, code, created["accessToken"])
    assert res.status_code == 200
    data = res.json()["data"]
    assert data["status"] == "ENDED"
    assert [a["actualSeconds"] for a in data["agendas"]] == [90, 59]
    assert data["timer"]["currentAgendaId"] is None
    assert data["transcripts"] == []


async def test_unauthorized(client: httpx.AsyncClient) -> None:
    created = await create(client)
    other = await create(client)
    code = created["meetingId"]
    cases = [
        None,  # 토큰 없음
        "not-a-jwt",
        other["accessToken"],  # 다른 회의 토큰
        create_access_token(999_999, code, "MEMBER"),  # 이 회의에 없는 참가자
    ]
    for token in cases:
        res = await get_state(client, code, token)
        assert res.status_code == 401, token
        assert res.json()["error"]["code"] == "UNAUTHORIZED"
    # 없는 회의도 그 회의 토큰이 있을 수 없으므로 401
    res = await get_state(client, "ZZZZZZ", create_access_token(1, "ZZZZZZ", "HOST"))
    assert res.status_code == 401
