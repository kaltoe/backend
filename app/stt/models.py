from pydantic import BaseModel


class Utterance(BaseModel):
    """발화 한 문장. 시각은 녹음(회의) 시작 기준 밀리초."""

    start_ms: int
    end_ms: int
    text: str
    speaker: str | None = None  # 실시간 STT 에는 화자가 없다
