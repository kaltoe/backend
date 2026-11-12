"""공통 응답 형식과 JSON 규칙 (API 명세서 > 공통 규약).

- JSON 키는 camelCase
- 시각은 ISO 8601 + 오프셋. DB 는 UTC 로 저장하고 응답은 한국 시간(+09:00)으로 보낸다
- 성공 응답: { "success": true, "data": …, "error": null }
"""

from datetime import datetime
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, PlainSerializer
from pydantic.alias_generators import to_camel

KST = ZoneInfo("Asia/Seoul")


def to_kst_iso(value: datetime) -> str:
    return value.astimezone(KST).isoformat(timespec="seconds")


# 응답 모델의 시각 필드에 쓴다: 2026-11-03T14:00:31+09:00
KstDatetime = Annotated[datetime, PlainSerializer(to_kst_iso, return_type=str)]


class CamelModel(BaseModel):
    """요청·응답 모델의 기본. 코드에서는 snake_case, JSON 에서는 camelCase."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, serialize_by_alias=True)


class Envelope[T](BaseModel):
    success: Literal[True] = True
    data: T
    error: None = None


def ok[T](data: T) -> Envelope[T]:
    return Envelope[T](data=data)
