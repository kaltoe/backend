"""Swagger(/docs) 문서. API 동작은 바꾸지 않고 설명·예시·오류 응답만 붙인다.

예시는 노션 API 명세서의 Example 을 따른다. 응답 모델을 바꾸면 여기 예시도 같이 고친다.
"""

from typing import Any, Literal

from fastapi import FastAPI
from fastapi.openapi.models import Example
from pydantic import BaseModel

from app.api.errors import VALIDATION_MESSAGE, ErrorCode, error_body

NOTION_API = "https://app.notion.com/p/API-4113a42323cb8059ae56d1b3d7379245"

SETUP, ENTRY, LIVE, HEALTH = "회의 셋업", "초대·입장", "라이브", "상태 확인"

TAGS = [
    {"name": SETUP, "description": "회의 생성(= 시작)과 안건. 토큰 없이 호출합니다."},
    {
        "name": ENTRY,
        "description": "초대 코드로 입장 정보를 보고 Sign In 해 참가자 토큰을 받습니다. 토큰 없이 호출합니다.",
    },
    {
        "name": LIVE,
        "description": "라이브 화면 복원과 진행자 제어(일시 정지·재개·안건 전환·종료). 참가자 토큰이 필요하고, "
        "제어 API는 진행자만 호출할 수 있습니다. 결과는 WS-01로 전원에게 퍼집니다.",
    },
    {"name": HEALTH, "description": "서버 상태 확인(배포·모니터링용)."},
]

DESCRIPTION = f"""
실시간 회의 페이스메이커 **칼퇴** API입니다. 기준은 [노션 API 명세서]({NOTION_API})이고, 이 문서는 그 요약입니다.

<details>
<summary><b>공통 규칙</b></summary>

- 응답 JSON 키는 camelCase, 시각은 한국 시간(+09:00) ISO 8601입니다.
- 성공: `{{ "success": true, "data": {{ … }}, "error": null }}`
- 실패: `{{ "success": false, "data": null, "error": {{ "code", "message", "field" }} }}`.
  `field`는 입력 오류(`VALIDATION_ERROR`)일 때만 옵니다. 예상하지 못한 오류는 500 `INTERNAL_ERROR`입니다.
- 토큰: API-02(회의 생성)·API-03(Sign In) 응답의 `accessToken`입니다.
  오른쪽 위 **Authorize**에 토큰만 넣으면 🔒 표시 API에 `Authorization: Bearer …`로 붙습니다.

</details>

<details>
<summary><b>WebSocket</b></summary>

Swagger에서는 호출할 수 없어 요약만 적습니다. 자세한 내용은 노션 WS-01·WS-02를 봅니다.

**WS-01 상태 동기화** `wss://…/ws/meetings/{{meetingId}}?token={{accessToken}}`
- 명령은 REST로 보내고, 결과는 이 소켓으로 전원에게 퍼집니다. 메시지는 모두 `{{ type, data, sentAt }}`입니다.
- 클라이언트 → 서버: `ping`(25초마다), `mic.status {{ state: ON | OFF | DENIED }}`
- 서버 → 클라이언트: `participant.joined` · `participant.left` · `participant.updated` · `meeting.paused` ·
  `meeting.resumed` · `agenda.changed` · `meeting.ended` · `timer.sync` · `transcript.partial` ·
  `transcript.final` · `stt.status` · `recording.status` · `error` · `pong`
- 끊겼다가 다시 붙으면 API-04로 놓친 내용을 채웁니다.

**WS-02 음성 송출** `wss://…/ws/meetings/{{meetingId}}/audio?token={{accessToken}}`
- 연결 직후 텍스트 프레임 한 번:
  `{{ "type": "audio.start", "sampleRate": 16000, "encoding": "LINEAR16", "channels": 1, "startOffsetMs": null }}`.
  재연결이면 `startOffsetMs`에 로컬 버퍼 첫 청크의 녹음 위치(최대 10분 전)를 넣습니다.
- 그 뒤 바이너리 PCM 청크(약 100~250ms)를 보냅니다. 서버는 이 채널로 아무것도 보내지 않습니다(이벤트는 WS-01).

**close code**
- `4401` 토큰 없음·만료. 재연결하지 않습니다
- `4404` 회의 없음
- `4409` (WS-02) 같은 참가자가 다른 기기에서 음성 연결해 이전 연결을 닫음
- `4410` 회의 종료. 재연결하지 않습니다
- `1008` (WS-02) `audio.start` 형식이 틀렸거나 `startOffsetMs`가 버퍼 범위 밖
- `1001` (WS-01) 60초 동안 메시지가 없음. 다시 연결해도 됩니다

</details>
"""


class ErrorDetail(BaseModel):
    code: str
    message: str
    field: str | None = None


class ErrorResponse(BaseModel):
    success: Literal[False]
    data: None
    error: ErrorDetail


Responses = dict[int | str, dict[str, Any]]


def success(data: dict[str, Any], status: int = 200) -> Responses:
    return {status: {"content": {"application/json": {"example": {"success": True, "data": data, "error": None}}}}}


def errors(*codes: ErrorCode, field: str | None = None) -> Responses:
    """실패 응답을 상태별로 묶어 코드마다 예시를 단다. field 는 VALIDATION_ERROR 예시에 쓸 입력 이름."""
    by_status: dict[int, list[ErrorCode]] = {}
    for code in codes:
        by_status.setdefault(code.status, []).append(code)

    def example(code: ErrorCode) -> dict[str, Any]:
        if code is ErrorCode.VALIDATION_ERROR:
            return {"summary": code.name, "value": error_body(code, VALIDATION_MESSAGE, field)}
        return {"summary": code.name, "value": error_body(code, code.default_message)}

    return {
        status: {
            "model": ErrorResponse,
            "description": " · ".join(c.name for c in group),
            "content": {"application/json": {"examples": {c.name: example(c) for c in group}}},
        }
        for status, group in by_status.items()
    }


def install_openapi(app: FastAPI) -> None:
    """FastAPI 기본 422 응답을 문서에서 뺀다. 입력 오류는 400 VALIDATION_ERROR 로 바꿔 보내기 때문이다(errors.py)."""
    generate = app.openapi

    def openapi() -> dict[str, Any]:
        if app.openapi_schema is None:
            schema = generate()
            for operations in schema["paths"].values():
                for operation in operations.values():
                    operation["responses"].pop("422", None)
            for name in ("HTTPValidationError", "ValidationError"):
                schema.get("components", {}).get("schemas", {}).pop(name, None)
        return app.openapi_schema or {}

    app.openapi = openapi  # type: ignore[method-assign]


# 예시 (노션 API 명세서 Example)

CODE = "K7Q2MX"
INVITE = {"code": CODE, "url": f"https://kaltoe.app/m/{CODE}", "expiresAt": "2026-11-17T14:00:00+09:00"}


def timer(**override: Any) -> dict[str, Any]:
    return {
        "serverNow": "2026-11-03T14:00:00+09:00",
        "status": "IN_PROGRESS",
        "plannedTotalSeconds": 1800,
        "totalElapsedSeconds": 0,
        "pausedTotalSeconds": 0,
        "currentAgendaId": 12,
        "agendaAllocatedSeconds": 600,
        "agendaElapsedSeconds": 0,
    } | override


AGENDAS = [
    {
        "agendaId": 12,
        "orderIndex": 0,
        "title": "지난주 캠페인 성과 리뷰",
        "keywords": ["CTR", "전환율"],
        "allocatedSeconds": 600,
        "status": "IN_PROGRESS",
    },
    {
        "agendaId": 13,
        "orderIndex": 1,
        "title": "4분기 예산 검토",
        "keywords": ["예산", "광고비"],
        "allocatedSeconds": 720,
        "status": "PENDING",
    },
    {
        "agendaId": 14,
        "orderIndex": 2,
        "title": "다음 주 액션 아이템",
        "keywords": ["담당자", "일정"],
        "allocatedSeconds": 480,
        "status": "PENDING",
    },
]

CREATE_REQUEST: dict[str, Example] = {
    "대면 회의": {
        "summary": "대면 회의(목표 30분, 안건 3개)",
        "value": {
            "title": "마케팅 전략 주간 회의",
            "targetTotalMinutes": 30,
            "meetingMode": "IN_PERSON",
            "attendeeCount": 4,
            "consentAgreed": True,
            "agendas": [
                {"title": "지난주 캠페인 성과 리뷰", "keywords": ["CTR", "전환율"], "allocatedMinutes": 10},
                {"title": "4분기 예산 검토", "keywords": ["예산", "광고비"], "allocatedMinutes": 12},
                {"title": "다음 주 액션 아이템", "keywords": ["담당자", "일정"], "allocatedMinutes": 8},
            ],
        },
    },
    "비대면 회의": {
        "summary": "비대면 회의(목표 시간·인원 생략)",
        "value": {
            "title": "주간 스탠드업",
            "meetingMode": "REMOTE",
            "consentAgreed": True,
            "agendas": [{"title": "진행 상황 공유", "keywords": [], "allocatedMinutes": 15}],
        },
    },
}
CREATED = {
    "meetingId": CODE,
    "title": "마케팅 전략 주간 회의",
    "meetingMode": "IN_PERSON",
    "status": "IN_PROGRESS",
    "invite": INVITE,
    "timer": timer(),
    "agendas": AGENDAS,
    "host": {"participantId": 1, "name": "진행자", "role": "HOST"},
    "accessToken": "eyJhbGciOi...",
    "hostRecoveryKey": "K7QM-2XPA",
}

ENTRY_INFO = {
    "meetingId": CODE,
    "title": "마케팅 전략 주간 회의",
    "status": "IN_PROGRESS",
    "connectedCount": 3,
    "expiresAt": "2026-11-17T14:00:00+09:00",
}

SIGN_IN_REQUEST: dict[str, Example] = {
    "새 참가자": {"summary": "이름만으로 입장", "value": {"name": "민지"}},
    "비밀번호": {
        "summary": "비밀번호(숫자 4자리)를 걸고 입장. 같은 이름·비밀번호로 다른 기기에서 이어받을 수 있음",
        "value": {"name": "민지", "password": "1234"},
    },
    "진행자 복구": {"summary": "진행자 복구 키로 진행자 권한을 되찾음", "value": {"hostRecoveryKey": "K7QM-2XPA"}},
}
SIGNED_IN = {
    "participantId": 7,
    "name": "민지",
    "role": "MEMBER",
    "isReturning": False,
    "meetingStatus": "IN_PROGRESS",
    "accessToken": "eyJhbGciOi...",
}

STATE = {
    "meetingId": CODE,
    "title": "마케팅 전략 주간 회의",
    "meetingMode": "IN_PERSON",
    "status": "IN_PROGRESS",
    "connectedCount": 3,
    "invite": INVITE,
    "timer": timer(serverNow="2026-11-03T14:00:31+09:00", totalElapsedSeconds=31, agendaElapsedSeconds=31),
    "agendas": [agenda | {"actualSeconds": None} for agenda in AGENDAS[:2]],
    "me": {"participantId": 7, "name": "민지", "role": "MEMBER", "micState": "ON"},
    "participants": [{"participantId": 1, "name": "진행자", "role": "HOST", "isConnected": True, "micState": "ON"}],
    "transcripts": [
        {
            "utteranceId": 301,
            "text": "지난주 캠페인 CTR이 2.4%로 목표보다 조금 높게 나왔어요.",
            "offsetMs": 17000,
            "agendaId": 12,
            "participantId": None,
            "speakerName": None,
        }
    ],
    "sttStatus": "OK",
    "recording": {"status": "RECORDING", "sourceParticipantId": 1},
    "offtopicStatus": "OK",
    "activeOfftopicEvent": None,
}

PAUSED = {
    "pausedAt": "2026-11-03T14:10:00+09:00",
    "timer": timer(
        serverNow="2026-11-03T14:10:00+09:00", status="PAUSED", totalElapsedSeconds=31, agendaElapsedSeconds=31
    ),
}
RESUMED = {
    "resumedAt": "2026-11-03T14:11:20+09:00",
    "timer": timer(serverNow="2026-11-03T14:11:20+09:00", totalElapsedSeconds=31, agendaElapsedSeconds=31),
}

NEXT_REQUEST: dict[str, Example] = {
    "다음 안건": {"summary": "지금 안건(12)을 끝내고 다음으로", "value": {"fromAgendaId": 12}}
}
AGENDA_CHANGED = {
    "previousAgenda": {"agendaId": 12, "actualSeconds": 710, "diffSeconds": 110, "outcome": "OVERTIME"},
    "currentAgenda": {"agendaId": 13, "orderIndex": 1, "title": "4분기 예산 검토", "allocatedSeconds": 720},
    "timer": timer(
        serverNow="2026-11-03T14:11:50+09:00",
        totalElapsedSeconds=710,
        currentAgendaId=13,
        agendaAllocatedSeconds=720,
        agendaElapsedSeconds=0,
    ),
}

ENDED = {
    "meetingId": CODE,
    "status": "ENDED",
    "endedAt": "2026-11-03T14:28:00+09:00",
    "endedBy": "HOST",
    "actualDurationSeconds": 1600,
}
