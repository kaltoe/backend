"""사담 판정(#23). PoC 2 최종 설정(OpenAI Decisions, gpt-6-luna)에 키워드를 더하고 질문을 2개로 바꿨다.

진행 중인 회의마다 OFFTOPIC_INTERVAL_SECONDS(기본 5초)마다 최근 30초 대화를 판정한다.
- 입력: 회의 제목, 전체 안건(현재 표시), 현재 안건과 그 키워드, 최근 30초 대화
- 질문 2개(predicate) 중 하나라도 확률 0.5 이상이면 '사담 높음'.
  2회 연속이면 offtopic_events 에 저장하고 offtopic.warning
- 경고 뒤 30초는 판정하지 않고, 연속 횟수는 0부터 다시 센다
- 건너뛰기: 일시 정지 중(진행 중 회의만 본다), 안건이 시작된 지 20초 안, 같은 안건에서 [관련 있는 얘기예요]를
  누른 뒤 60초(#24), 최근 30초 대화가 15자 미만. 건너뛰면 연속이 끊긴다(안건이 바뀌면 20초를 건너뛰므로 0부터 센다)
- 창(window)은 지금 안건이 시작된 뒤로만 잡는다. 창과 1초 이상 겹친 확정 자막은 문장 전체를 넣는다.
  대면은 기준 기기 자막만 저장되고, 비대면은 기기별 자막이 녹음 위치 순으로 섞인다(말한 사람 이름을 붙인다)
- 모델 호출은 실패하면 1·2·4초 뒤 다시 시도한다(요청당 10초 제한). 그래도 실패한 판정이 3회 연속이면
  offtopicStatus ERROR, 한 번 성공하면 OK. 바뀔 때 offtopic.status

window_text 에는 대화 내용이 들어가므로 로그로 남기지 않는다.
판정 상태(연속 횟수·실패 횟수·쉬는 시각)는 서버 메모리에 둔다(WS-01 manager 와 같음).
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import lru_cache
from typing import Literal

from openai import AsyncOpenAI
from sqlalchemy import func, select

from app.config import get_settings
from app.db.models import Agenda, Meeting, MeetingStatus, OfftopicEvent, Utterance, UtteranceSource
from app.db.session import SessionFactory, get_session_factory
from app.meetings import timer
from app.realtime.events import OfftopicStatusChanged, OfftopicWarning
from app.realtime.manager import manager
from app.stt.live import spawn

logger = logging.getLogger(__name__)

MODEL = "gpt-6-luna"
REQUEST_TIMEOUT_SECONDS = 10.0
RETRY_DELAYS = (1.0, 2.0, 4.0)  # 최초 1회 + 이만큼 쉬고 다시
WINDOW_MS = 30_000
MIN_OVERLAP_MS = 1_000  # 창과 이만큼 겹친 문장은 전문을 넣는다
MIN_CHARS = 15  # 창 글자 수가 이보다 적으면 판정하지 않는다
AGENDA_GRACE_MS = 20_000  # 안건이 시작된 뒤 판정하지 않는 시간
THRESHOLD = 0.5
STREAK = 2
FAIL_LIMIT = 3
AFTER_WARNING = timedelta(seconds=30)  # 경고 뒤 판정하지 않는 시간
DISMISS_QUIET = timedelta(seconds=60)
AUTO_CLOSE_SECONDS = 5

# 둘 중 하나라도 THRESHOLD 이상이면 사담 높음
QUESTIONS = {
    "q_current": "최근 대화는 현재 안건 혹은 키워드와 관련된 내용이 아니다.",
    "q_chat": "최근 대화는 회의 주제와 무관한 사적인 잡담이다.",
}

OfftopicState = Literal["OK", "ERROR"]


@dataclass
class Judge:
    highs: int = 0  # 연속으로 사담 높음이 나온 횟수
    failures: int = 0  # 연속 실패 횟수
    state: OfftopicState = "OK"
    quiet_until: datetime | None = None  # 경고 뒤 쉬는 끝 시각
    busy: bool = False  # 판정(재시도 포함) 중이면 다음 회차를 건너뛰어 겹치지 않게


judges: dict[str, Judge] = {}


def render_input(meeting_title: str, agendas: list[str], current: int, keywords: list[str], conversation: str) -> str:
    listing = "  ".join(f"{i + 1}. {name}" + (" (현재)" if i == current else "") for i, name in enumerate(agendas))
    return (
        f"회의 제목: {meeting_title}\n전체 안건: {listing}\n현재 안건: {agendas[current]}\n"
        f"현재 안건 키워드: {', '.join(keywords) or '없음'}\n최근 대화:\n{conversation}"
    )


@lru_cache
def openai_client() -> AsyncOpenAI:
    """판정마다 새로 만들면 HTTP 연결이 쌓이므로 프로세스에 하나만 둔다."""
    return AsyncOpenAI(api_key=get_settings().openai_api_key, max_retries=0)


async def ask_model(text: str) -> dict[str, float]:
    """질문별 확률. 거절한 질문은 빠진다. 호출이 실패하면 예외."""
    decision = await openai_client().decisions.create(
        model=MODEL,
        input=text,
        timeout=REQUEST_TIMEOUT_SECONDS,
        questions=[{"type": "predicate", "name": n, "instructions": q} for n, q in QUESTIONS.items()],
    )
    return {a.name: a.probability for a in decision.answers if a.type == "predicate" and a.name is not None}


async def ask_with_retry(text: str) -> dict[str, float]:
    for delay in RETRY_DELAYS:
        try:
            return await ask_model(text)
        except Exception as exc:
            logger.info("사담 판정 호출 실패, %.0f초 뒤 다시: %s", delay, type(exc).__name__)
            await asyncio.sleep(delay)
    return await ask_model(text)


async def set_state(meeting_id: str, judge: Judge, state: OfftopicState) -> None:
    if judge.state != state:
        judge.state = state
        await manager.broadcast(meeting_id, "offtopic.status", OfftopicStatusChanged(state=state))


def window(lines: list[tuple[str | None, str, int, int | None]], lo: int, hi: int) -> str:
    """창 [lo, hi] 와 1초 이상 겹친 문장. lines 는 (말한 사람, 텍스트, 시작 ms, 길이 ms)."""
    picked = [
        f"[{speaker}] {text}" if speaker else text
        for speaker, text, start, duration in lines
        if text and min(start + (duration or 0), hi) - max(start, lo) >= MIN_OVERLAP_MS
    ]
    return "\n".join(picked)


async def check(sessions: SessionFactory, meeting_id: str, now: datetime) -> None:
    """회의 하나를 한 번 판정한다."""
    judge = judges.setdefault(meeting_id, Judge())
    async with sessions() as session:
        meeting = await session.get(Meeting, meeting_id)
        if meeting is None or meeting.status != MeetingStatus.IN_PROGRESS:
            return
        agenda = await timer.current_agenda(session, meeting_id)
        if agenda is None:
            return
        position = timer.timeline_ms(meeting, now)
        agenda_start = agenda.recording_start_ms or 0
        if position - agenda_start < AGENDA_GRACE_MS or (judge.quiet_until is not None and now < judge.quiet_until):
            judge.highs = 0
            return
        dismissed = await session.scalar(
            select(OfftopicEvent.id)
            .where(OfftopicEvent.agenda_id == agenda.id, OfftopicEvent.dismissed_at >= now - DISMISS_QUIET)
            .limit(1)
        )
        lo = max(agenda_start, position - WINDOW_MS)  # 창이 이전 안건으로 넘어가지 않게
        rows = await session.execute(
            select(Utterance.speaker_label, Utterance.text, Utterance.offset_ms, Utterance.duration_ms)
            .where(
                Utterance.meeting_id == meeting_id,
                Utterance.source == UtteranceSource.REALTIME.value,
                Utterance.offset_ms < position,
                Utterance.offset_ms + func.coalesce(Utterance.duration_ms, 0) > lo,
            )
            .order_by(Utterance.offset_ms, Utterance.id)
        )
        lines = [(r.speaker_label, r.text, r.offset_ms, r.duration_ms) for r in rows]
        conversation = window(lines, lo, position)
        agendas = list(
            await session.scalars(select(Agenda).where(Agenda.meeting_id == meeting_id).order_by(Agenda.order_index))
        )
        names = [a.title for a in agendas]
        current = next(i for i, a in enumerate(agendas) if a.id == agenda.id)
        title, keywords, agenda_id, agenda_title = meeting.title, list(agenda.keywords), agenda.id, agenda.title

    chars = sum(len(line) for line in conversation.splitlines())
    if dismissed is not None or chars < MIN_CHARS:
        judge.highs = 0  # 건너뛴 구간을 사이에 두고 연속으로 세지 않는다
        return

    try:
        probs = await ask_with_retry(render_input(title, names, current, keywords, conversation))
    except Exception as exc:
        judge.failures += 1
        logger.warning("사담 판정 실패(%d회 연속): %s %s", judge.failures, meeting_id, type(exc).__name__)
        if judge.failures >= FAIL_LIMIT:
            await set_state(meeting_id, judge, "ERROR")
        return
    judge.failures = 0
    await set_state(meeting_id, judge, "OK")

    score = max(probs.values(), default=None)  # 모델이 둘 다 거절하면 판정 없음
    if score is None or score < THRESHOLD:
        judge.highs = 0
        return
    judge.highs += 1
    if judge.highs < STREAK:
        return
    judge.highs = 0

    async with sessions() as session:
        # 모델 호출(재시도 포함 수십 초) 사이에 멈추거나 끝나거나 안건이 바뀌었으면 지난 판정으로 경고하지 않는다
        meeting = await session.get(Meeting, meeting_id)
        current_agenda = await timer.current_agenda(session, meeting_id)
        moved = meeting is None or meeting.status != MeetingStatus.IN_PROGRESS
        if moved or current_agenda is None or current_agenda.id != agenda_id:
            return
        judge.quiet_until = now + AFTER_WARNING
        event = OfftopicEvent(
            meeting_id=meeting_id, agenda_id=agenda_id, score=score, window_text=conversation, detected_at=now
        )
        session.add(event)
        await session.flush()
        event_id = event.id
        await session.commit()
    warning = OfftopicWarning(
        event_id=event_id,
        agenda_id=agenda_id,
        agenda_title=agenda_title,
        score=score,
        detected_at=now,
        auto_close_seconds=AUTO_CLOSE_SECONDS,
    )
    await manager.broadcast(meeting_id, "offtopic.warning", warning)


async def safe_check(sessions: SessionFactory, meeting_id: str, now: datetime, judge: Judge) -> None:
    try:
        await check(sessions, meeting_id, now)
    except Exception:
        logger.exception("사담 판정 중 오류: %s", meeting_id)
    finally:
        judge.busy = False


async def sweep(sessions: SessionFactory, now: datetime) -> None:
    """진행 중인 회의마다 판정을 띄운다. 재시도로 아직 끝나지 않은 회의는 이번 회차를 건너뛴다."""
    async with sessions() as session:
        live = set(await session.scalars(select(Meeting.id).where(Meeting.status == MeetingStatus.IN_PROGRESS.value)))
    for gone in set(judges) - live:  # 끝나거나 멈춘 회의는 잊는다(재개하면 처음부터 센다)
        judges.pop(gone, None)
    for meeting_id in live:
        judge = judges.setdefault(meeting_id, Judge())
        if not judge.busy:
            judge.busy = True
            spawn(safe_check(sessions, meeting_id, now, judge))


async def sweep_loop() -> None:
    settings = get_settings()
    if not settings.openai_api_key:  # 로컬처럼 키가 없으면 판정하지 않는다(상태는 OK 그대로)
        logger.info("OPENAI_API_KEY 가 없어 사담 판정을 끕니다")
        return
    while True:
        await asyncio.sleep(settings.offtopic_interval_seconds)
        try:
            await sweep(get_session_factory(), timer.utcnow())
        except Exception:
            logger.exception("사담 판정 검사 실패")


def state(meeting_id: str) -> OfftopicState:
    """API-04 offtopicStatus."""
    judge = judges.get(meeting_id)
    return judge.state if judge else "OK"
