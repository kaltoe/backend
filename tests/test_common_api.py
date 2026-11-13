from datetime import UTC, datetime, timedelta
from typing import Annotated

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from pydantic import AfterValidator

from app.api.auth import CurrentCaller, CurrentHost, create_access_token, read_token
from app.api.errors import ApiError, ErrorCode, field_path, install_error_handlers
from app.api.rate_limit import SlidingWindowLimiter, entry_limiter, limit_entry_requests
from app.api.schemas import CamelModel, Envelope, KstDatetime, ok


def title_rule(value: str) -> str:
    if len(value.strip()) < 1:
        raise ValueError("안건 내용을 입력해주세요.")
    return value


class AgendaIn(CamelModel):
    title: Annotated[str, AfterValidator(title_rule)]
    allocated_minutes: int


class MeetingIn(CamelModel):
    agendas: list[AgendaIn]


class Out(CamelModel):
    meeting_id: str
    started_at: KstDatetime


app = FastAPI()
install_error_handlers(app)


@app.post("/meetings")
def create(body: MeetingIn) -> Envelope[Out]:
    return ok(Out(meeting_id="K7Q2MX", started_at=datetime(2026, 11, 3, 5, 0, 31, tzinfo=UTC)))


@app.get("/fail")
def fail() -> None:
    raise ApiError(ErrorCode.MEETING_NOT_FOUND)


@app.get("/boom")
def boom() -> None:
    raise RuntimeError("secret detail")


@app.get("/meetings/{meeting_id}/me")
def me(caller: CurrentCaller) -> dict[str, object]:
    return {"participantId": caller.participant_id, "role": caller.role}


@app.post("/meetings/{meeting_id}/pause")
def pause(caller: CurrentHost) -> dict[str, bool]:
    return {"ok": True}


@app.get("/meetings/{meeting_id}/entry", dependencies=[Depends(limit_entry_requests)])
def entry(meeting_id: str) -> dict[str, str]:
    return {"meetingId": meeting_id}


client = TestClient(app, raise_server_exceptions=False)


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_success_envelope_camel_case_and_kst() -> None:
    resp = client.post("/meetings", json={"agendas": [{"title": "리뷰", "allocatedMinutes": 10}]})
    assert resp.status_code == 200
    assert resp.json() == {
        "success": True,
        "data": {"meetingId": "K7Q2MX", "startedAt": "2026-11-03T14:00:31+09:00"},
        "error": None,
    }


def test_validation_error_becomes_400_with_field_path_and_message() -> None:
    body = {"agendas": [{"title": "리뷰", "allocatedMinutes": 10}, {"title": " ", "allocatedMinutes": 5}]}
    resp = client.post("/meetings", json=body)
    assert resp.status_code == 400
    assert resp.json() == {
        "success": False,
        "data": None,
        "error": {"code": "VALIDATION_ERROR", "message": "안건 내용을 입력해주세요.", "field": "agendas[1].title"},
    }


def test_validation_error_without_custom_message() -> None:
    resp = client.post("/meetings", json={"agendas": [{"title": "리뷰"}]})
    assert resp.status_code == 400
    assert resp.json()["error"] == {
        "code": "VALIDATION_ERROR",
        "message": "입력값을 확인해주세요.",
        "field": "agendas[0].allocatedMinutes",
    }


def test_api_error_uses_table_status_and_message() -> None:
    resp = client.get("/fail")
    assert resp.status_code == 404
    assert resp.json() == {
        "success": False,
        "data": None,
        "error": {"code": "MEETING_NOT_FOUND", "message": "존재하지 않는 회의예요."},
    }


def test_unexpected_error_is_500_without_details() -> None:
    resp = client.get("/boom")
    assert resp.status_code == 500
    assert resp.json()["error"]["code"] == "INTERNAL_ERROR"
    assert "secret" not in resp.text


def test_every_error_code_has_status_and_message() -> None:
    assert len(ErrorCode) == 21
    for code in ErrorCode:
        assert code.status in (400, 401, 403, 404, 409, 410, 429, 500)
        assert code.default_message


@pytest.mark.parametrize(
    ("loc", "expected"),
    [
        (("body", "agendas", 1, "title"), "agendas[1].title"),
        (("body", "title"), "title"),
        (("query", "source"), "source"),
        (("body",), None),
    ],
)
def test_field_path(loc: tuple[int | str, ...], expected: str | None) -> None:
    assert field_path(loc) == expected


def test_token_roundtrip_and_payload() -> None:
    token = create_access_token(7, "K7Q2MX", "MEMBER")
    caller = read_token(token, "K7Q2MX")
    assert caller is not None
    assert (caller.participant_id, caller.meeting_id, caller.role) == (7, "K7Q2MX", "MEMBER")


def test_token_for_other_meeting_or_expired_is_rejected() -> None:
    assert read_token(create_access_token(7, "K7Q2MX", "MEMBER"), "ABCDEF") is None
    old = datetime.now(UTC) - timedelta(days=15)
    assert read_token(create_access_token(7, "K7Q2MX", "MEMBER", now=old), "K7Q2MX") is None
    assert read_token("not-a-token", "K7Q2MX") is None


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer"},
        {"Authorization": "Basic abc"},
        bearer(create_access_token(7, "ABCDEF", "MEMBER")),  # 다른 회의 토큰
    ],
)
def test_missing_or_wrong_token_is_401(headers: dict[str, str]) -> None:
    resp = client.get("/meetings/K7Q2MX/me", headers=headers)
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "UNAUTHORIZED"


def test_participant_token_passes() -> None:
    resp = client.get("/meetings/K7Q2MX/me", headers=bearer(create_access_token(7, "K7Q2MX", "MEMBER")))
    assert resp.status_code == 200
    assert resp.json() == {"participantId": 7, "role": "MEMBER"}


def test_host_only() -> None:
    member = client.post("/meetings/K7Q2MX/pause", headers=bearer(create_access_token(7, "K7Q2MX", "MEMBER")))
    assert member.status_code == 403
    assert member.json()["error"]["code"] == "HOST_ONLY"
    host = client.post("/meetings/K7Q2MX/pause", headers=bearer(create_access_token(1, "K7Q2MX", "HOST")))
    assert host.status_code == 200


def test_entry_rate_limit_121st_request_is_429() -> None:
    entry_limiter.reset()
    for _ in range(120):
        assert client.get("/meetings/K7Q2MX/entry").status_code == 200
    resp = client.get("/meetings/K7Q2MX/entry")
    assert resp.status_code == 429
    assert resp.json()["error"]["code"] == "TOO_MANY_REQUESTS"
    entry_limiter.reset()


def test_sliding_window_frees_after_a_minute() -> None:
    limiter = SlidingWindowLimiter(limit=2, window=60)
    assert limiter.hit("ip", now=0) and limiter.hit("ip", now=1)
    assert not limiter.hit("ip", now=59)
    assert limiter.hit("ip", now=60.5)  # 0초 요청이 창 밖으로 나감
    assert limiter.hit("other", now=59)  # IP별로 따로 센다


def test_token_signed_with_other_key_is_rejected() -> None:
    import jwt

    forged = jwt.encode(
        {"participantId": 1, "meetingId": "K7Q2MX", "role": "HOST", "exp": datetime.now(UTC) + timedelta(days=1)},
        "guessed-key-that-is-long-enough-for-hs256",
        algorithm="HS256",
    )
    assert read_token(forged, "K7Q2MX") is None


def test_app_refuses_to_start_without_jwt_secret_outside_local(monkeypatch: pytest.MonkeyPatch) -> None:
    from app import main
    from app.config import get_settings

    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("JWT_SECRET", "")
    get_settings.cache_clear()
    try:
        with pytest.raises(RuntimeError, match="JWT_SECRET"):
            main.create_app()
        monkeypatch.setenv("JWT_SECRET", "x" * 48)
        get_settings.cache_clear()
        main.create_app()
    finally:
        get_settings.cache_clear()
