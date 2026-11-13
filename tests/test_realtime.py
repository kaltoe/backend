import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any, cast

import httpx
import pytest
from fastapi import FastAPI
from httpx_ws import AsyncWebSocketSession, WebSocketDisconnect, aconnect_ws
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Meeting
from app.log_filters import DropQueryString, strip_query
from app.meetings.timer import utcnow
from app.realtime import manager as manager_module
from app.realtime import ws as ws_module
from app.realtime.manager import manager
from tests.conftest import ws_client

CREATE = {
    "title": "마케팅 전략 주간 회의",
    "consentAgreed": True,
    "agendas": [{"title": "리뷰", "keywords": [], "allocatedMinutes": 10}],
}


class Room:
    """회의 하나와 그 참가자 토큰. connect()로 WS-01에 붙는다."""

    def __init__(self, client: httpx.AsyncClient, created: dict[str, Any]) -> None:
        self.client = client
        self.code: str = created["meetingId"]
        self.host_id: int = created["host"]["participantId"]
        self.host_token: str = created["accessToken"]

    async def member(self, name: str) -> tuple[int, str]:
        res = await self.client.post(f"/api/v1/meetings/{self.code}/sign-in", json={"name": name})
        data = res.json()["data"]
        return data["participantId"], data["accessToken"]

    def connect(self, token: str | None, code: str | None = None) -> Any:
        query = f"?token={token}" if token is not None else ""
        return aconnect_ws(
            f"/ws/meetings/{code or self.code}{query}", self.client, keepalive_ping_interval_seconds=None
        )


@asynccontextmanager
async def room(app: FastAPI) -> AsyncIterator[Room]:
    async with ws_client(app) as client:
        created = (await client.post("/api/v1/meetings", json=CREATE)).json()["data"]
        yield Room(client, created)


async def receive(ws: AsyncWebSocketSession, timeout: float = 2.0) -> dict[str, Any]:
    message: dict[str, Any] = await ws.receive_json(timeout=timeout)
    return message


async def nothing_arrives(ws: AsyncWebSocketSession, wait: float = 0.3) -> bool:
    try:
        await ws.receive_json(timeout=wait)
    except TimeoutError:
        return True
    return False


async def ready(ws: AsyncWebSocketSession) -> list[dict[str, Any]]:
    """ping 을 보내 pong 이 올 때까지 읽는다. 서버가 이 연결의 인증을 끝냈다는 뜻이고, 그 전에 온 메시지를 돌려준다.

    테스트는 DB 세션 하나를 같이 쓰므로, 연결을 하나씩 열고 ready 로 기다린 뒤 다음 연결을 연다.
    """
    await ws.send_json({"type": "ping"})
    before: list[dict[str, Any]] = []
    while (message := await receive(ws))["type"] != "pong":
        before.append(message)
    return before


def types(messages: list[dict[str, Any]]) -> list[str]:
    return [m["type"] for m in messages]


async def close_code(ws: AsyncWebSocketSession) -> int:
    with pytest.raises(WebSocketDisconnect) as exc:
        await ws.receive_json(timeout=2)
    return exc.value.code


# 연결 오류 close code
async def test_close_codes(app: FastAPI, session: AsyncSession) -> None:
    async with room(app) as r:
        other = (await r.client.post("/api/v1/meetings", json=CREATE)).json()["data"]
        for token, code, expected in [
            (None, None, 4401),
            ("not-a-token", None, 4401),
            (other["accessToken"], None, 4401),  # 다른 회의 토큰
            (r.host_token, "ZZZZZZ", 4404),
        ]:
            async with r.connect(token, code) as ws:
                assert await close_code(ws) == expected

        meeting = await session.get(Meeting, r.code)
        assert meeting is not None
        expires_at = meeting.expires_at
        meeting.expires_at = utcnow() - timedelta(seconds=1)  # 만료(API-01 의 410 MEETING_EXPIRED)
        await session.flush()
        async with r.connect(r.host_token) as ws:
            assert await close_code(ws) == 4410
        meeting.expires_at = expires_at

        meeting.status, meeting.ended_at, meeting.ended_by = "ENDED", utcnow(), "HOST"
        await session.flush()
        async with r.connect(r.host_token) as ws:
            assert await close_code(ws) == 4410


async def test_stalled_send_times_out_and_drops_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    """받지 않는 클라이언트 하나 때문에 브로드캐스트가 멈추지 않는다."""
    monkeypatch.setattr(manager_module, "SEND_TIMEOUT_SECONDS", 0.05)

    class StalledSocket:
        async def send_json(self, message: Any) -> None:
            await asyncio.Event().wait()  # 영원히 끝나지 않는 전송

    reader = asyncio.create_task(asyncio.Event().wait())
    connection = manager_module.Connection(
        websocket=cast(Any, StalledSocket()), member=manager_module.Member(1, "민지", "MEMBER"), reader=reader
    )
    assert await connection.send({"type": "pong"}) is False
    with pytest.raises(asyncio.CancelledError):
        await reader


# participant.joined / left / connectedCount
async def test_joined_left_and_connected_count(app: FastAPI) -> None:
    async with room(app) as r:
        member_id, member_token = await r.member("민지")
        async with r.connect(r.host_token) as host:
            [joined] = await ready(host)
            assert joined["type"] == "participant.joined"
            assert joined["data"] == {"participantId": r.host_id, "name": "진행자", "role": "HOST", "connectedCount": 1}
            assert joined["sentAt"].endswith("+09:00")

            async with r.connect(member_token) as phone:
                expected = {"participantId": member_id, "name": "민지", "role": "MEMBER", "connectedCount": 2}
                assert [m["data"] for m in await ready(phone)] == [expected]
                assert [m["data"] for m in await ready(host)] == [expected]

                # 같은 참가자의 두 번째 기기: 연결은 받되 인원은 그대로, joined 도 다시 안 나간다
                async with r.connect(member_token) as laptop:
                    assert await ready(laptop) == []
                    assert await ready(host) == []
                    assert manager.count(r.code) == 2
                # 한 기기만 끊기면 아직 접속 중
                assert await ready(host) == []
                assert manager.is_connected(r.code, member_id)

            left = await receive(host)
            assert left["type"] == "participant.left"
            assert left["data"] == {"participantId": member_id, "connectedCount": 1}
            assert not manager.is_connected(r.code, member_id)
            assert manager.count(r.code) == 1


async def test_ping_pong_only_to_sender(app: FastAPI) -> None:
    async with room(app) as r:
        _, member_token = await r.member("민지")
        async with r.connect(r.host_token) as host:
            await ready(host)
            async with r.connect(member_token) as member:
                await ready(member)
                assert types(await ready(host)) == ["participant.joined"]  # 민지의 ping 에 대한 pong 은 오지 않는다
                await member.send_json({"type": "ping"})
                pong = await receive(member)
                assert (pong["type"], pong["data"]) == ("pong", None)
                assert await nothing_arrives(host)


async def test_heartbeat_timeout(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ws_module, "HEARTBEAT_TIMEOUT_SECONDS", 1.0)
    async with room(app) as r:
        member_id, member_token = await r.member("민지")
        async with r.connect(r.host_token) as host:
            await ready(host)
            async with r.connect(member_token) as member:
                await ready(member)
                await ready(host)
                # 진행자는 계속 ping 을 보내고, 민지는 아무것도 보내지 않는다
                for _ in range(4):
                    await asyncio.sleep(0.15)
                    assert await ready(host) == []
                assert await close_code(member) == 1001
            left = await receive(host)
            assert left["type"] == "participant.left" and left["data"]["participantId"] == member_id


async def test_mic_status_updates_everyone(app: FastAPI) -> None:
    async with room(app) as r:
        member_id, member_token = await r.member("민지")
        assert manager.mic_state(r.code, member_id) == "ON"  # 처음엔 ON
        async with r.connect(r.host_token) as host:
            await ready(host)
            async with r.connect(member_token) as member:
                await ready(member)
                await ready(host)
                await member.send_json({"type": "mic.status", "data": {"state": "DENIED"}})
                for ws in (host, member):
                    updated = await receive(ws)
                    assert updated["type"] == "participant.updated"
                    assert updated["data"] == {"participantId": member_id, "micState": "DENIED"}
        # 연결이 끊겨도 마이크 상태는 남는다(API-04 participants.micState)
        assert manager.mic_state(r.code, member_id) == "DENIED"


@pytest.mark.parametrize(
    "message",
    [
        "not json",
        '"ping"',
        '{"type": "unknown"}',
        '{"type": "mic.status", "data": {"state": "MUTED"}}',
        '{"type": "mic.status"}',
    ],
)
async def test_invalid_message_gets_error(app: FastAPI, message: str) -> None:
    async with room(app) as r, r.connect(r.host_token) as host:
        await ready(host)
        await host.send_text(message)
        error = await receive(host)
        assert error["type"] == "error"
        assert error["data"] == {"code": "INVALID_MESSAGE", "message": "처리할 수 없는 메시지예요."}
        # 연결은 그대로다
        await host.send_json({"type": "ping"})
        assert (await receive(host))["type"] == "pong"


async def test_binary_message_gets_error(app: FastAPI) -> None:
    async with room(app) as r, r.connect(r.host_token) as host:
        await ready(host)
        await host.send_bytes(b"\x00\x01")
        assert (await receive(host))["data"]["code"] == "INVALID_MESSAGE"


async def test_timer_sync_only_while_in_progress(
    app: FastAPI, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ws_module, "TIMER_SYNC_SECONDS", 0.2)
    async with room(app) as r, r.connect(r.host_token) as host:
        await ready(host)
        sync = await receive(host)
        assert sync["type"] == "timer.sync"
        assert set(sync["data"]["timer"]) == {
            "serverNow",
            "status",
            "plannedTotalSeconds",
            "totalElapsedSeconds",
            "pausedTotalSeconds",
            "currentAgendaId",
            "agendaAllocatedSeconds",
            "agendaElapsedSeconds",
        }
        assert sync["data"]["timer"]["status"] == "IN_PROGRESS"

        meeting = await session.get(Meeting, r.code)
        assert meeting is not None
        meeting.status, meeting.paused_at = "PAUSED", utcnow()
        await session.flush()
        await asyncio.sleep(0.25)  # 이미 출발한 한 번은 흘려 보낸다
        while not await nothing_arrives(host, wait=0.5):
            pass
        assert await nothing_arrives(host, wait=0.5)


async def test_other_meeting_is_isolated(app: FastAPI) -> None:
    async with room(app) as a, room(app) as b, a.connect(a.host_token) as wa:
        await ready(wa)
        async with b.connect(b.host_token) as wb:
            await ready(wb)
            assert await nothing_arrives(wa)  # b 의 participant.joined 는 a 에 오지 않는다
            await manager.broadcast(a.code, "meeting.paused", {"pausedAt": "2026-11-03T14:05:00+09:00"})
            assert (await receive(wa))["type"] == "meeting.paused"
            assert await nothing_arrives(wb)


def test_access_log_drops_query_string() -> None:
    assert strip_query("/ws/meetings/K7Q2MX?token=eyJhbGciOi") == "/ws/meetings/K7Q2MX"
    assert strip_query("/health") == "/health"
    assert strip_query(200) == 200

    record = logging.LogRecord(
        "uvicorn.error", logging.INFO, "", 0, '%s - "WebSocket %s" [accepted]', ("1.2.3.4:5", "/ws/m/A?token=s"), None
    )
    DropQueryString().filter(record)
    assert record.getMessage() == '1.2.3.4:5 - "WebSocket /ws/m/A" [accepted]'


def test_log_filters_installed_on_uvicorn_loggers() -> None:
    from app.main import create_app

    create_app()
    for name in ("uvicorn.access", "uvicorn.error"):
        assert sum(isinstance(f, DropQueryString) for f in logging.getLogger(name).filters) == 1
