"""STT 엔진 연동. 엔진을 바꿔도 나머지 코드가 그대로이도록 결과는 Utterance 로 통일한다.

- 회의 중 실시간 자막·사담 판단: 리턴제로 스트리밍 (sommers_ko, domain=MEETING)
- 회의 후 화자 분리: CLOVA Speech 장문 인식
근거: PoC 1 결과 보고서
"""

from app.stt.models import Utterance

__all__ = ["Utterance"]
