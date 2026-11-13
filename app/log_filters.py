"""접속 로그에서 쿼리 문자열을 지운다 (WS-01 비고, 결정 사항).

WebSocket 토큰이 ?token= 으로 오므로, uvicorn 접속 로그에 주소를 그대로 남기면 로그를 공유할 때 토큰이 새어 나간다.
HTTP 접속 로그는 uvicorn.access, WebSocket 접속 로그는 uvicorn.error 로거에 남는다.
"""

import logging


def strip_query(value: object) -> object:
    if isinstance(value, str) and value.startswith("/") and "?" in value:
        return value.split("?", 1)[0]
    return value


class DropQueryString(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(strip_query(arg) for arg in record.args)
        return True


def install_log_filters() -> None:
    for name in ("uvicorn.access", "uvicorn.error"):
        target = logging.getLogger(name)
        if not any(isinstance(f, DropQueryString) for f in target.filters):
            target.addFilter(DropQueryString())
