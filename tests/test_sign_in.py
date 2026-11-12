from typing import Any

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import read_token
from app.api.errors import ApiError, ErrorCode
from app.api.rate_limit import LIMIT, entry_limiter
from app.db.models import Meeting, Participant
from app.meetings import attempts
from app.meetings.sign_in import _new_member, normalize_recovery_key
from app.meetings.timer import utcnow
from app.realtime.manager import manager

CREATE = {
    "title": "마케팅 전략 주간 회의",
    "consentAgreed": True,
    "agendas": [{"title": "리뷰", "keywords": [], "allocatedMinutes": 10}],
}


class Clock:
    """실패 잠금이 쓰는 time.monotonic 을 테스트에서 움직인다."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    c = Clock()
    monkeypatch.setattr(attempts.time, "monotonic", c)
    return c


@pytest.fixture
async def meeting(client: httpx.AsyncClient) -> dict[str, Any]:
    res = await client.post("/api/v1/meetings", json=CREATE)
    assert res.status_code == 201
    data: dict[str, Any] = res.json()["data"]
    return data


async def sign_in(client: httpx.AsyncClient, code: str, **body: Any) -> httpx.Response:
    return await client.post(f"/api/v1/meetings/{code}/sign-in", json=body)


async def participant_count(session: AsyncSession, code: str) -> int:
    count = await session.scalar(select(func.count()).select_from(Participant).where(Participant.meeting_id == code))
    return count or 0


def error_code(res: httpx.Response) -> str:
    code: str = res.json()["error"]["code"]
    return code


# 규칙 1
async def test_new_name_creates_member(
    client: httpx.AsyncClient, session: AsyncSession, meeting: dict[str, Any]
) -> None:
    code = meeting["meetingId"]
    res = await sign_in(client, code, name="  민지 ")
    assert res.status_code == 200
    data = res.json()["data"]
    assert {k: v for k, v in data.items() if k not in ("participantId", "accessToken")} == {
        "name": "민지",
        "role": "MEMBER",
        "isReturning": False,
        "meetingStatus": "IN_PROGRESS",
    }
    caller = read_token(data["accessToken"], code)
    assert caller is not None and caller.participant_id == data["participantId"] and not caller.is_host
    member = await session.get(Participant, data["participantId"])
    assert member is not None and member.password_hash is None


async def test_new_name_with_password_stores_hash(
    client: httpx.AsyncClient, session: AsyncSession, meeting: dict[str, Any]
) -> None:
    res = await sign_in(client, meeting["meetingId"], name="민지", password="1234")
    member = await session.get(Participant, res.json()["data"]["participantId"])
    assert member is not None and member.password_hash is not None
    assert "1234" not in member.password_hash


async def test_meeting_status_paused(client: httpx.AsyncClient, session: AsyncSession, meeting: dict[str, Any]) -> None:
    row = await session.get(Meeting, meeting["meetingId"])
    assert row is not None
    row.status, row.paused_at = "PAUSED", utcnow()
    await session.flush()
    res = await sign_in(client, meeting["meetingId"], name="민지")
    assert res.json()["data"]["meetingStatus"] == "PAUSED"


# 규칙 2
async def test_returning_with_password(client: httpx.AsyncClient, meeting: dict[str, Any]) -> None:
    code = meeting["meetingId"]
    first = (await sign_in(client, code, name="민지", password="1234")).json()["data"]

    again = await sign_in(client, code, name="민지", password="1234")
    assert again.status_code == 200
    assert again.json()["data"]["participantId"] == first["participantId"]
    assert again.json()["data"]["isReturning"] is True

    for wrong in ({"password": "9999"}, {}):
        res = await sign_in(client, code, name="민지", **wrong)
        assert res.status_code == 401
        assert res.json()["error"] == {"code": "INVALID_PASSWORD", "message": "비밀번호가 맞지 않아요."}


# 규칙 3
async def test_take_over_name_without_password_when_disconnected(
    client: httpx.AsyncClient, session: AsyncSession, meeting: dict[str, Any]
) -> None:
    code = meeting["meetingId"]
    first = (await sign_in(client, code, name="민지")).json()["data"]

    res = await sign_in(client, code, name="민지", password="5678")
    assert res.status_code == 200
    data = res.json()["data"]
    assert (data["participantId"], data["isReturning"]) == (first["participantId"], True)
    member = await session.get(Participant, first["participantId"])
    assert member is not None and member.password_hash is None  # 요청의 비밀번호는 저장하지 않는다
    assert await participant_count(session, code) == 2  # 진행자 + 민지


async def test_name_without_password_is_taken_while_connected(
    client: httpx.AsyncClient, meeting: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    code = meeting["meetingId"]
    first = (await sign_in(client, code, name="민지")).json()["data"]
    monkeypatch.setattr(manager, "is_connected", lambda m, p: (m, p) == (code, first["participantId"]))
    res = await sign_in(client, code, name="민지")
    assert res.status_code == 409
    assert res.json()["error"] == {"code": "NAME_TAKEN", "message": "이미 참여 중인 이름이에요."}


# 규칙 4
async def test_host_name_cannot_be_taken(client: httpx.AsyncClient, meeting: dict[str, Any]) -> None:
    res = await sign_in(client, meeting["meetingId"], name="진행자")
    assert res.status_code == 409
    assert error_code(res) == "NAME_TAKEN"


async def test_same_new_name_at_once_creates_one(session: AsyncSession, meeting: dict[str, Any]) -> None:
    code = meeting["meetingId"]
    session.add(Participant(meeting_id=code, name="민지", role="MEMBER"))
    await session.flush()
    with pytest.raises(ApiError) as exc:
        await _new_member(session, code, "민지", None)
    assert exc.value.code is ErrorCode.NAME_TAKEN


# 진행자 복구
async def test_host_recovery_reuses_host(
    client: httpx.AsyncClient, session: AsyncSession, meeting: dict[str, Any]
) -> None:
    code = meeting["meetingId"]
    messy = meeting["hostRecoveryKey"].replace("-", "").lower()
    before = await participant_count(session, code)

    res = await sign_in(client, code, hostRecoveryKey=f" {messy} ", name="", password="x")
    assert res.status_code == 200
    data = res.json()["data"]
    assert data["participantId"] == meeting["host"]["participantId"]
    assert (data["name"], data["role"], data["isReturning"]) == ("진행자", "HOST", True)
    caller = read_token(data["accessToken"], code)
    assert caller is not None and caller.is_host
    assert await participant_count(session, code) == before
    # 이전 기기의 진행자 토큰도 그대로 쓸 수 있다
    assert read_token(meeting["accessToken"], code) is not None


@pytest.mark.parametrize("key", ["AAAA-AAAA", "short", "K7QM-2XPA-EXTRA"])
async def test_wrong_recovery_key(client: httpx.AsyncClient, meeting: dict[str, Any], key: str) -> None:
    res = await sign_in(client, meeting["meetingId"], hostRecoveryKey=key)
    assert res.status_code == 401
    assert res.json()["error"] == {"code": "INVALID_RECOVERY_KEY", "message": "진행자 복구 키가 맞지 않아요."}


def test_normalize_recovery_key() -> None:
    assert normalize_recovery_key("k7qm2xpa") == "K7QM-2XPA"
    assert normalize_recovery_key("K7QM - 2XPA") == "K7QM-2XPA"
    assert normalize_recovery_key("K0QM-2XPA") is None  # 0 은 쓰지 않는 문자


# 잠금
async def test_password_lock_after_five_failures(
    client: httpx.AsyncClient, meeting: dict[str, Any], clock: Clock
) -> None:
    code = meeting["meetingId"]
    await sign_in(client, code, name="민지", password="1234")
    for _ in range(5):
        assert (await sign_in(client, code, name="민지", password="0000")).status_code == 401

    locked = await sign_in(client, code, name="민지", password="1234")
    assert locked.status_code == 429
    assert locked.json()["error"] == {"code": "TOO_MANY_ATTEMPTS", "message": "잠시 후 다시 시도해주세요."}
    # 다른 이름은 막히지 않는다
    assert (await sign_in(client, code, name="준호")).status_code == 200

    clock.now += 299
    assert (await sign_in(client, code, name="민지", password="1234")).status_code == 429
    clock.now += 1
    assert (await sign_in(client, code, name="민지", password="1234")).status_code == 200


async def test_success_resets_password_failures(
    client: httpx.AsyncClient, meeting: dict[str, Any], clock: Clock
) -> None:
    code = meeting["meetingId"]
    await sign_in(client, code, name="민지", password="1234")
    for _ in range(4):
        await sign_in(client, code, name="민지", password="0000")
    assert (await sign_in(client, code, name="민지", password="1234")).status_code == 200
    for _ in range(4):
        assert (await sign_in(client, code, name="민지", password="0000")).status_code == 401
    assert (await sign_in(client, code, name="민지", password="1234")).status_code == 200


async def test_recovery_key_lock_per_meeting(client: httpx.AsyncClient, meeting: dict[str, Any], clock: Clock) -> None:
    code = meeting["meetingId"]
    other = (await client.post("/api/v1/meetings", json=CREATE)).json()["data"]
    for _ in range(5):
        assert (await sign_in(client, code, hostRecoveryKey="AAAA-AAAA")).status_code == 401

    res = await sign_in(client, code, hostRecoveryKey=meeting["hostRecoveryKey"])
    assert res.status_code == 429 and error_code(res) == "TOO_MANY_ATTEMPTS"
    assert (await sign_in(client, other["meetingId"], hostRecoveryKey=other["hostRecoveryKey"])).status_code == 200
    # 이름으로 들어오는 참가자는 막히지 않는다
    assert (await sign_in(client, code, name="민지")).status_code == 200

    clock.now += 300
    assert (await sign_in(client, code, hostRecoveryKey=meeting["hostRecoveryKey"])).status_code == 200


# 검증·입장 불가
@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({"name": "가" * 11}, "name"),
        ({"name": "   "}, "name"),
        ({}, "name"),
        ({"name": "민지", "password": "12a4"}, "password"),
        ({"name": "민지", "password": "12345"}, "password"),
    ],
)
async def test_validation(client: httpx.AsyncClient, meeting: dict[str, Any], body: dict[str, Any], field: str) -> None:
    res = await sign_in(client, meeting["meetingId"], **body)
    assert res.status_code == 400
    assert res.json()["error"] == {"code": "VALIDATION_ERROR", "message": "입력값을 확인해주세요.", "field": field}


async def test_empty_password_means_none(
    client: httpx.AsyncClient, session: AsyncSession, meeting: dict[str, Any]
) -> None:
    res = await sign_in(client, meeting["meetingId"], name="민지", password="", hostRecoveryKey="")
    assert res.status_code == 200
    member = await session.get(Participant, res.json()["data"]["participantId"])
    assert member is not None and member.password_hash is None


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"status": "ENDED", "ended_by": "HOST"}, "MEETING_ENDED"),
        ({"expired": True}, "MEETING_EXPIRED"),
    ],
)
async def test_not_joinable(
    client: httpx.AsyncClient, session: AsyncSession, meeting: dict[str, Any], change: dict[str, Any], code: str
) -> None:
    row = await session.get(Meeting, meeting["meetingId"])
    assert row is not None
    now = utcnow()
    if change.pop("expired", False):
        row.expires_at = now
    else:
        row.status, row.ended_by, row.ended_at = change["status"], change["ended_by"], now
    await session.flush()
    for body in ({"name": "민지"}, {"hostRecoveryKey": meeting["hostRecoveryKey"]}):
        res = await sign_in(client, meeting["meetingId"], **body)
        assert res.status_code == 410 and error_code(res) == code


async def test_unknown_meeting(client: httpx.AsyncClient) -> None:
    res = await sign_in(client, "ZZZZZZ", name="민지")
    assert res.status_code == 404 and error_code(res) == "MEETING_NOT_FOUND"


async def test_shares_ip_counter_with_entry(client: httpx.AsyncClient, meeting: dict[str, Any]) -> None:
    for _ in range(LIMIT):
        entry_limiter.hit("127.0.0.1")
    res = await sign_in(client, meeting["meetingId"], name="민지")
    assert res.status_code == 429 and error_code(res) == "TOO_MANY_REQUESTS"
