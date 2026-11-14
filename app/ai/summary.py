"""안건별 3줄 요약(#27). 회의가 끝나면 완료된 안건마다 3줄 요약을 만들어 agendas.summary_lines 에 저장한다.

- 모델: gpt-6-luna(사담 판정과 같은 키). Responses API 로 3줄짜리 JSON 을 받는다
- 언제(2026-11 결정): 대면·비대면 모두 종료 직후 실시간 발화로 먼저 만든다. 대면은 CLOVA 화자 분리가
  끝나면(INT-01 DONE) 그 발화로 다시 만들어 덮어쓴다. 다시 만들기가 실패하면 먼저 만든 요약을 그대로 둔다
- 대상: 결과 화면처럼 완료(DONE)된 안건만. 발언이 없던 안건은 []
- summary_status PROCESSING → DONE, 모델 호출이 하나라도 실패하면 FAILED(API-12 로 다시 시도)

5초마다 "요약할 차례인 회의"(종료, summary PROCESSING)를 찾아 만든다. 재시도·서버 재시작도 같은 곳에서 처리된다.
덮어쓰기는 CLOVA 콜백이 refresh_later() 로 띄운다.
ponytail: 덮어쓰기 대기는 메모리에만 있어 그 사이 서버가 재시작되면 실시간 발화 요약이 남는다. 필요하면 표시 컬럼을 둔다
"""

import asyncio
import json
import logging
from typing import Any

from openai import AsyncOpenAI
from sqlalchemy import select

from app.config import get_settings
from app.db.models import (
    Agenda,
    AgendaStatus,
    Meeting,
    MeetingMode,
    MeetingStatus,
    PostprocessStatus,
    SummaryStatus,
    Utterance,
    UtteranceSource,
)
from app.db.session import SessionFactory, get_session_factory
from app.stt.live import spawn

logger = logging.getLogger(__name__)

MODEL = "gpt-6-luna"
REQUEST_TIMEOUT_SECONDS = 60.0
SWEEP_SECONDS = 5.0
REFRESH_WAIT_SECONDS = 300  # 첫 요약이 끝나기를 기다리는 최대 시간

PROMPT = (
    "너는 회의록 작성 도우미야. 한 안건에서 나온 발언을 읽고 핵심을 정확히 3줄로 요약해. "
    "각 줄은 한 문장이고 '~요'로 끝나는 존댓말로 써. 숫자·결정 사항·담당자는 빠뜨리지 마. "
    "발언에 없는 내용은 지어내지 마."
)
SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"lines": {"type": "array", "items": {"type": "string"}, "minItems": 3, "maxItems": 3}},
    "required": ["lines"],
    "additionalProperties": False,
}

running: set[str] = set()


async def ask_model(meeting_title: str, agenda_title: str, keywords: list[str], conversation: str) -> list[str]:
    """3줄 요약. 실패하면 예외."""
    client = AsyncOpenAI(api_key=get_settings().openai_api_key, max_retries=2)
    user = (
        f"회의 제목: {meeting_title}\n안건: {agenda_title}\n키워드: {', '.join(keywords) or '없음'}\n\n"
        f"발언:\n{conversation}"
    )
    resp = await client.responses.create(
        model=MODEL,
        input=[{"role": "system", "content": PROMPT}, {"role": "user", "content": user}],
        text={"format": {"type": "json_schema", "name": "summary", "schema": SCHEMA, "strict": True}},
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    lines = json.loads(resp.output_text)["lines"]
    if not (isinstance(lines, list) and len(lines) == 3 and all(isinstance(x, str) and x.strip() for x in lines)):
        raise ValueError("3줄 요약 형식이 아님")
    return [x.strip() for x in lines]


async def summarize(sessions: SessionFactory, meeting_id: str, refresh: bool = False) -> None:
    """회의 하나의 요약. 보통은 summary_status 가 PROCESSING 일 때만 만든다.

    refresh 면 CLOVA 결과로 다시 만들어 덮어쓴다(이미 DONE·FAILED 인 대면 회의). 실패하면 그대로 둔다.
    """
    if meeting_id in running:
        return
    running.add(meeting_id)
    try:
        async with sessions() as session:
            meeting = await session.get(Meeting, meeting_id)
            if meeting is None:
                return
            in_person = meeting.meeting_mode == MeetingMode.IN_PERSON
            # 대면은 CLOVA 화자 분리 결과가 있으면 그것, 없거나 실패면 실시간 발화
            postprocessed = in_person and meeting.postprocess_status == PostprocessStatus.DONE
            if refresh:
                if not postprocessed or meeting.summary_status == SummaryStatus.PROCESSING:
                    return  # 아직 첫 요약 차례면 요약 루프가 CLOVA 결과로 만든다
            elif meeting.summary_status != SummaryStatus.PROCESSING:
                return
            source = UtteranceSource.POSTPROCESS if postprocessed else UtteranceSource.REALTIME
            title = meeting.title
            agendas = list(
                await session.scalars(
                    select(Agenda)
                    .where(Agenda.meeting_id == meeting_id, Agenda.status == AgendaStatus.DONE.value)
                    .order_by(Agenda.order_index)
                )
            )
            rows = await session.execute(
                select(Utterance.agenda_id, Utterance.speaker_label, Utterance.text)
                .where(Utterance.meeting_id == meeting_id, Utterance.source == source.value)
                .order_by(Utterance.offset_ms, Utterance.id)
            )
            talk: dict[int, list[str]] = {}
            for agenda_id, speaker, text in rows:
                if agenda_id is not None and text.strip():
                    talk.setdefault(agenda_id, []).append(f"[{speaker}] {text}" if speaker else text)
            jobs = [(a.id, a.title, list(a.keywords), "\n".join(talk.get(a.id, []))) for a in agendas]

        async def one(agenda_title: str, keywords: list[str], conversation: str) -> list[str]:
            if not conversation:  # 발언이 없던 안건
                return []
            return await ask_model(title, agenda_title, keywords, conversation)

        try:
            results = await asyncio.gather(*(one(t, k, c) for _, t, k, c in jobs))
        except Exception as exc:
            logger.warning("요약 생성 실패(refresh=%s): %s %s", refresh, meeting_id, type(exc).__name__)
            if refresh:  # 먼저 만든 실시간 발화 요약을 그대로 둔다
                return
            results = None

        async with sessions() as session:
            meeting = await session.get(Meeting, meeting_id)
            if meeting is None:
                return
            if results is None:
                meeting.summary_status = SummaryStatus.FAILED.value
            else:
                for (agenda_id, *_), lines in zip(jobs, results, strict=True):
                    agenda = await session.get(Agenda, agenda_id)
                    if agenda is not None:
                        agenda.summary_lines = lines
                meeting.summary_status = SummaryStatus.DONE.value
            await session.commit()
    finally:
        running.discard(meeting_id)


async def due(sessions: SessionFactory) -> list[str]:
    """요약할 차례인 회의: 종료, 요약 PROCESSING. 대면도 CLOVA 를 기다리지 않는다."""
    async with sessions() as session:
        return list(
            await session.scalars(
                select(Meeting.id).where(
                    Meeting.status == MeetingStatus.ENDED.value,
                    Meeting.summary_status == SummaryStatus.PROCESSING.value,
                )
            )
        )


async def safe_summarize(sessions: SessionFactory, meeting_id: str, refresh: bool = False) -> None:
    try:
        await summarize(sessions, meeting_id, refresh)
    except Exception:
        logger.exception("요약 처리 중 오류: %s", meeting_id)


async def refresh_after(sessions: SessionFactory, meeting_id: str) -> None:
    """CLOVA 결과가 저장된 뒤 부른다. 첫 요약이 아직 돌고 있으면 끝날 때까지 기다렸다가 다시 만든다."""
    for _ in range(REFRESH_WAIT_SECONDS):
        if meeting_id not in running:
            break
        await asyncio.sleep(1)
    await safe_summarize(sessions, meeting_id, refresh=True)


def refresh_later(meeting_id: str) -> None:
    """INT-01 콜백이 DONE 을 저장한 뒤 부른다."""
    spawn(refresh_after(get_session_factory(), meeting_id))


async def sweep_loop() -> None:
    while True:
        await asyncio.sleep(SWEEP_SECONDS)
        try:
            sessions = get_session_factory()
            for meeting_id in await due(sessions):
                if meeting_id not in running:
                    spawn(safe_summarize(sessions, meeting_id))
        except Exception:
            logger.exception("요약 검사 실패")
