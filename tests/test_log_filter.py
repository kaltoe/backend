import logging

from app.main import strip_query


def _msg(fmt: str, *args: object) -> str:
    record = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, fmt, args, None)
    strip_query(record)
    return record.getMessage()


def test_http_access_log_drops_query() -> None:
    msg = _msg('%s - "%s %s HTTP/%s" %d', "1.2.3.4:5", "GET", "/ws/meetings/1?token=secret", "1.1", 101)
    assert "secret" not in msg
    assert "/ws/meetings/1" in msg


def test_websocket_log_drops_query() -> None:
    assert "secret" not in _msg('%s - "WebSocket %s" [accepted]', "1.2.3.4:5", "/ws?token=secret")
