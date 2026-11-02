from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_broadcast_to_same_meeting() -> None:
    with (
        client.websocket_connect("/ws/meetings/m1") as host,
        client.websocket_connect("/ws/meetings/m1") as guest,
    ):
        assert host.receive_json() == {"type": "participant_count", "count": 1}
        assert host.receive_json() == {"type": "participant_count", "count": 2}
        assert guest.receive_json() == {"type": "participant_count", "count": 2}

        host.send_json({"type": "agenda_changed", "agenda_index": 1})
        assert host.receive_json() == {"type": "agenda_changed", "agenda_index": 1}
        assert guest.receive_json() == {"type": "agenda_changed", "agenda_index": 1}


def test_other_meeting_is_isolated() -> None:
    with (
        client.websocket_connect("/ws/meetings/a") as a,
        client.websocket_connect("/ws/meetings/b") as b,
    ):
        assert a.receive_json()["count"] == 1
        assert b.receive_json()["count"] == 1

        a.send_json({"type": "ping"})
        assert a.receive_json() == {"type": "ping"}
        b.send_json({"type": "pong"})
        assert b.receive_json() == {"type": "pong"}  # a 의 메시지는 b 에 오지 않는다
