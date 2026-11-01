import httpx
import pytest
from gateway.launch import service_state


def test_launcher_reuses_only_healthy_expected_local_service():
    def handler(req):
        return httpx.Response(200, json={'status': 'ok', 'backend': 'pytorch', 'max_streams': 4})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert service_state(client, 8000) == 'ready'


def test_launcher_windows_slow_refusal_is_missing_not_unknown():
    def handler(req):
        raise httpx.ConnectTimeout('Windows refusal exceeds HTTP connect timeout')
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert service_state(client, 8001, tcp_probe=lambda port: False) == 'missing'


def test_launcher_does_not_replace_unrelated_port_owner():
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={'other': True}))) as client:
        with pytest.raises(RuntimeError, match='unrecognized'):
            service_state(client, 8000)


def test_launcher_waits_for_a_port_that_is_still_shutting_down(monkeypatch):
    from gateway import launch
    answers = iter([RuntimeError('Port 8000 did not answer; not replacing an unknown process')] * 2 + ['missing'])
    def fake(client, port, tcp_probe=None):
        a = next(answers)
        if isinstance(a, Exception):
            raise a
        return a
    monkeypatch.setattr(launch, 'service_state', fake)
    assert launch.state_with_retry(None, 8000, pause=0) == 'missing'


def test_launcher_still_refuses_an_unrelated_service(monkeypatch):
    import pytest
    from gateway import launch
    def fake(client, port, tcp_probe=None):
        raise RuntimeError('Port 8001 belongs to an unrecognized service')
    monkeypatch.setattr(launch, 'service_state', fake)
    with pytest.raises(RuntimeError):
        launch.state_with_retry(None, 8001, pause=0)
