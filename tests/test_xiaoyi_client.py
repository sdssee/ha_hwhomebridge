"""Transport tests guard against repeated physical commands after network errors."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

path = Path(__file__).resolve().parents[1] / "xiaoyi_skill/close-desk-lamp/scripts/close_lamp.py"
spec = importlib.util.spec_from_file_location("xiaoyi_client_test", path)
client = importlib.util.module_from_spec(spec)
spec.loader.exec_module(client)


class Response:
    status = 200
    will_close = False

    def __init__(self, value):
        self.body = json.dumps(value).encode()

    def read(self, limit):
        return self.body[:limit]


class Connection:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.sock = SimpleNamespace(settimeout=Mock())
        self.requests = []
        self.closed = False

    def request(self, method, path, **kw):
        self.requests.append((method, path, kw))

    def getresponse(self):
        result = next(self.responses)
        if isinstance(result, Exception):
            raise result
        return Response(result)

    def close(self):
        self.closed = True


HEALTH = {"ok": True, "api_version": 1, "service": "hwhomebridge_home"}


def settings(monkeypatch):
    monkeypatch.setattr(client, "load_settings", lambda: (
        SimpleNamespace(hostname="example.test", port=None, path="/private-control"), {}))
    monkeypatch.setattr(client.socket, "getaddrinfo", lambda *a, **kw: [
        (client.socket.AF_INET, client.socket.SOCK_STREAM, 6, "", ("192.0.2.1", 443)),
        (client.socket.AF_INET, client.socket.SOCK_STREAM, 6, "", ("192.0.2.2", 443)),
    ])
    raw_sockets = []
    def make_socket(*args):
        sock = SimpleNamespace(settimeout=Mock(), connect=Mock(), close=Mock())
        raw_sockets.append(sock)
        return sock
    monkeypatch.setattr(client.socket, "socket", make_socket)
    context = SimpleNamespace(wrap_socket=Mock(side_effect=lambda sock, **kw: sock))
    monkeypatch.setattr(client.ssl, "create_default_context", lambda: context)
    return raw_sockets, context


def test_only_get_preflight_retries_then_post_reuses_same_connection(monkeypatch):
    raw_sockets, context = settings(monkeypatch)
    connections = [Connection([TimeoutError()]), Connection([{"wrong": True}]), Connection([HEALTH, {"ok": True}])]
    factory = iter(connections)
    monkeypatch.setattr(client.http.client, "HTTPSConnection", lambda *a, **k: next(factory))
    assert client.request({"command": "catalog"})["ok"]
    assert [[r[0] for r in c.requests] for c in connections] == [["GET"], ["GET"], ["GET", "POST"]]
    assert all(c.closed for c in connections)
    assert [s.connect.call_args.args[0] for s in raw_sockets] == [
        ("192.0.2.1", 443), ("192.0.2.2", 443), ("192.0.2.1", 443)]
    assert all(call.kwargs["server_hostname"] == "example.test" for call in context.wrap_socket.call_args_list)


def test_post_timeout_is_never_retried(monkeypatch):
    settings(monkeypatch)
    conn = Connection([HEALTH, TimeoutError()])
    factory = Mock(return_value=conn)
    monkeypatch.setattr(client.http.client, "HTTPSConnection", factory)
    with pytest.raises(TimeoutError):
        client.request({"command": "control"})
    assert [r[0] for r in conn.requests] == ["GET", "POST"]
    factory.assert_called_once()
    assert conn.closed


def test_health_failure_never_sends_post(monkeypatch):
    settings(monkeypatch)
    connections = [Connection([{"ok": True}]) for _ in range(3)]
    factory = iter(connections)
    monkeypatch.setattr(client.http.client, "HTTPSConnection", lambda *a, **k: next(factory))
    with pytest.raises(ConnectionError):
        client.request({"command": "control"})
    assert all([r[0] for r in c.requests] == ["GET"] for c in connections)


def test_cli_does_not_default_catalog_to_a_control(monkeypatch, capsys):
    request = Mock(return_value={"ok": True, "devices": []})
    monkeypatch.setattr(client, "request", request)
    assert client.main(["--command", "catalog", "--domain", "cover"]) == 0
    request.assert_called_once_with({"command": "catalog", "domain": "cover"})
    assert json.loads(capsys.readouterr().out)["ok"]
