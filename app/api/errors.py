"""API 명세서 메인의 에러 코드 표와 공통 실패 응답.

실패 응답: { "success": false, "data": null, "error": { "code", "message", "field"? } }
INT-01(CLOVA 콜백)과 WebSocket 은 이 형식을 쓰지 않는다.
"""

import logging
from enum import Enum, unique
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

# 표에서 message 가 "-" 인 코드는 화면에 띄우지 않는 오류라 짧은 설명만 둔다
GENERIC_MESSAGE = "일시적인 오류가 발생했어요. 잠시 후 다시 시도해주세요."
VALIDATION_MESSAGE = "입력값을 확인해주세요."


# 값에 코드 이름을 넣는다. (상태, 문구)만 두면 같은 값의 코드가 별칭으로 합쳐진다(429 두 개)
@unique
class ErrorCode(Enum):
    VALIDATION_ERROR = ("VALIDATION_ERROR", 400, VALIDATION_MESSAGE)
    AGENDA_TIME_MISMATCH = ("AGENDA_TIME_MISMATCH", 400, "안건 시간 합계가 목표 시간과 달라요.")
    CONSENT_REQUIRED = ("CONSENT_REQUIRED", 400, "음성 수집 및 AI 분석에 동의해주세요.")
    UNAUTHORIZED = ("UNAUTHORIZED", 401, "인증 정보가 없거나 만료됐어요.")
    INVALID_PASSWORD = ("INVALID_PASSWORD", 401, "비밀번호가 맞지 않아요.")
    INVALID_RECOVERY_KEY = ("INVALID_RECOVERY_KEY", 401, "진행자 복구 키가 맞지 않아요.")
    HOST_ONLY = ("HOST_ONLY", 403, "일시 정지, 안건 전환, 종료는 진행자만 할 수 있어요.")
    MEETING_NOT_FOUND = ("MEETING_NOT_FOUND", 404, "존재하지 않는 회의예요.")
    SHARE_NOT_FOUND = ("SHARE_NOT_FOUND", 404, "만료되었거나 잘못된 링크예요.")
    OFFTOPIC_EVENT_NOT_FOUND = ("OFFTOPIC_EVENT_NOT_FOUND", 404, "사담 경고를 찾을 수 없어요.")
    NAME_TAKEN = ("NAME_TAKEN", 409, "이미 참여 중인 이름이에요.")
    INVALID_STATE = ("INVALID_STATE", 409, "지금 상태에서는 할 수 없는 요청이에요.")
    LAST_AGENDA = ("LAST_AGENDA", 409, "마지막 안건이에요.")
    AGENDA_CHANGED = ("AGENDA_CHANGED", 409, "안건이 이미 바뀌었어요.")
    MEETING_EXPIRED = ("MEETING_EXPIRED", 410, "만료된 회의 코드예요.")
    MEETING_ENDED = ("MEETING_ENDED", 410, "이미 끝난 회의예요.")
    SHARE_EXPIRED = ("SHARE_EXPIRED", 410, "만료되었거나 잘못된 링크예요.")
    RECORDING_EXPIRED = ("RECORDING_EXPIRED", 410, "녹음 보관 기간이 지났어요.")
    TOO_MANY_ATTEMPTS = ("TOO_MANY_ATTEMPTS", 429, "잠시 후 다시 시도해주세요.")
    TOO_MANY_REQUESTS = ("TOO_MANY_REQUESTS", 429, "잠시 후 다시 시도해주세요.")
    INTERNAL_ERROR = ("INTERNAL_ERROR", 500, GENERIC_MESSAGE)

    @property
    def status(self) -> int:
        return self.value[1]

    @property
    def default_message(self) -> str:
        return self.value[2]


class ApiError(Exception):
    """이 예외를 던지면 공통 실패 응답으로 바뀐다. message 를 생략하면 표의 기본 문구."""

    def __init__(self, code: ErrorCode, message: str | None = None, field: str | None = None) -> None:
        super().__init__(code.name)
        self.code = code
        self.message = message or code.default_message
        self.field = field


def error_body(code: ErrorCode, message: str, field: str | None = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code.name, "message": message}
    if field is not None:
        error["field"] = field
    return {"success": False, "data": None, "error": error}


def field_path(loc: tuple[int | str, ...]) -> str | None:
    """('body', 'agendas', 1, 'title') → 'agendas[1].title'"""
    parts = list(loc)
    if parts and parts[0] in ("body", "query", "path", "header", "cookie"):
        parts = parts[1:]
    path = ""
    for part in parts:
        if isinstance(part, int):
            path += f"[{part}]"
        else:
            path += f".{part}" if path else str(part)
    return path or None


def validation_message(err: dict[str, Any]) -> str:
    # 스키마의 검증 함수가 ValueError("문구")로 던진 문구는 그대로 보여 준다
    if err.get("type") == "value_error":
        cause = (err.get("ctx") or {}).get("error")
        if cause is not None and str(cause):
            return str(cause)
    return VALIDATION_MESSAGE


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def handle_api_error(_: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(error_body(exc.code, exc.message, exc.field), status_code=exc.code.status)

    @app.exception_handler(RequestValidationError)
    async def handle_validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        # FastAPI 기본 422 를 명세의 400 VALIDATION_ERROR 로 바꾼다. 첫 오류 하나만 알린다
        err = exc.errors()[0] if exc.errors() else {}
        body = error_body(ErrorCode.VALIDATION_ERROR, validation_message(err), field_path(tuple(err.get("loc", ()))))
        return JSONResponse(body, status_code=400)

    @app.exception_handler(Exception)
    async def handle_unexpected(_: Request, exc: Exception) -> JSONResponse:
        logger.exception("처리하지 못한 오류", exc_info=exc)
        return JSONResponse(error_body(ErrorCode.INTERNAL_ERROR, GENERIC_MESSAGE), status_code=500)
