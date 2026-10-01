from unittest.mock import Mock

from app.services.connection_manager import IDLE_AFTER_SECONDS, ConnectionManager


def test_is_active_expires_and_is_cleared_on_removal(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(ConnectionManager, "clock", staticmethod(lambda: now[0]))
    manager = ConnectionManager()
    ws = Mock()
    manager.add_connection(1, ws)

    assert not manager.is_active(1)
    manager.set_activity(ws, True)
    assert manager.is_active(1)
    now[0] += IDLE_AFTER_SECONDS
    assert not manager.is_active(1)

    manager.set_activity(ws, True)
    manager.remove_connection_by_websocket(ws)
    assert not manager.is_active(1)
    assert ws not in manager.last_active


def test_activity_of_an_unregistered_socket_is_ignored():
    manager = ConnectionManager()
    ws = Mock()
    manager.set_activity(ws, True)
    assert ws not in manager.last_active
