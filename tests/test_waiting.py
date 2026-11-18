"""대기(WAITING) 회의.

- #66 입장·상태 조회: 대기 화면(진행자·참가자)을 그릴 값이 노션 API-01·03·04 비고와 같아야 한다
- #67 막을 것: 대기 중에는 상태 변경 API·자동 종료·음성 수신이 돌지 않고, 결과 시간은 시작 시각부터 센다
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import to_kst_iso
from app.config import get_settings
from app.db.models import Meeting
from app.meetings import auto_end, timer
from app.realtime import audio
from app.realtime import ws as ws_module
from tests.test_agendas import T0, THREE_AGENDAS, at, bearer, go_next
from tests.test_audio import CHUNK, frames, send_chunk
from tests.test_audio import connect as audio_connect
from tests.test_audio import start as audio_start
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


# --- 대기 중 막을 것(#67) ---


@pytest.mark.parametrize(
    ("path", "body"),
    [("pause", None), ("resume", None), ("agendas/next", "first"), ("end", None)],  # API-05·06·07·08
)
async def test_state_changes_are_409_while_waiting(
    client: httpx.AsyncClient, session: AsyncSession, path: str, body: str | None
) -> None:
    created = await waiting(client)
    code = created["meetingId"]
    json = {"fromAgendaId": created["agendas"][0]["agendaId"]} if body else None
    res = await client.post(f"/api/v1/meetings/{code}/{path}", json=json, headers=bearer(created["accessToken"]))
    assert (res.status_code, res.json()["error"]["code"]) == (409, "INVALID_STATE")
    meeting = await session.get(Meeting, code)
    assert meeting is not None
    await session.refresh(meeting)
    assert (meeting.status, meeting.started_at, meeting.ended_at) == ("WAITING", None, None)  # 아무것도 바뀌지 않음


async def test_waiting_meeting_is_not_auto_ended(client: httpx.AsyncClient, session: AsyncSession) -> None:
    @asynccontextmanager
    async def borrowed() -> AsyncIterator[AsyncSession]:
        yield session

    code = (await waiting(client))["meetingId"]
    auto_end.empty_since.clear()
    try:
        now = timer.utcnow()
        for later in (0, 601, 6 * 3600):  # 접속자 0명으로 10분 넘게, 목표 + 60분도 훌쩍 넘김
            assert await auto_end.sweep(borrowed, now + timedelta(seconds=later)) == []
        assert code not in auto_end.empty_since  # 부재 시간도 세지 않는다
    finally:
        auto_end.empty_since.clear()


async def test_audio_while_waiting_is_dropped(app: FastAPI, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(get_settings(), "recordings_dir", str(tmp_path))
    audio.audio_sockets.clear()
    try:
        at(monkeypatch, 0)
        async with room(app, start=False) as r:
            wav = tmp_path / r.code / f"{r.host_id}.wav"
            async with audio_connect(r, r.host_token) as ws:
                await ws.send_json(audio_start())
                at(monkeypatch, 10)
                await ws.send_bytes(CHUNK)  # 대기 중 → 버린다(파일도 만들지 않음)
                await asyncio.sleep(0.2)
                assert not wav.exists()

                at(monkeypatch, 20)
                await r.client.post(f"/api/v1/meetings/{r.code}/start", headers=bearer(r.host_token))
                at(monkeypatch, 20.25)  # 시작 250ms 뒤
                await send_chunk(ws, wav, 250)  # 첫 소리가 녹음 위치 0~250ms 에 들어간다
            assert frames(wav) == CHUNK
    finally:
        audio.audio_sockets.clear()


async def test_full_flow_counts_from_start(
    client: httpx.AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    at(monkeypatch, 0)
    created = await waiting(client)
    code, token = created["meetingId"], created["accessToken"]
    review = created["agendas"][0]["agendaId"]
    await sign_in(client, code, "민지")

    at(monkeypatch, 300)  # 5분 기다렸다가 시작
    await client.post(f"/api/v1/meetings/{code}/start", headers=bearer(token))
    at(monkeypatch, 300 + 610)
    await go_next(client, created, review)
    at(monkeypatch, 300 + 1200)
    await client.post(f"/api/v1/meetings/{code}/end", headers=bearer(token))

    result = (await client.get(f"/api/v1/meetings/{code}/result", headers=bearer(token))).json()["data"]
    assert (result["startedAt"], result["endedAt"]) == (
        to_kst_iso(T0 + timedelta(seconds=300)),
        to_kst_iso(T0 + timedelta(seconds=1500)),
    )
    assert result["time"]["actualDurationSeconds"] == 1200  # 대기 5분은 들어가지 않는다
    assert result["time"]["pausedTotalSeconds"] == 0  # 대기는 일시 정지로도 세지 않는다
    assert [(a["actualSeconds"], a["recordingStartMs"], a["recordingEndMs"]) for a in result["agendas"]] == [
        (610, 0, 610_000),
        (590, 610_000, 1_200_000),
    ]
