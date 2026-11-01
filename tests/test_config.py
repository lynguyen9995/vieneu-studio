import pytest
from gateway.config import load_settings


def test_generate_secret_once_and_default_deny_origins(tmp_path):
    first = load_settings(tmp_path)
    second = load_settings(tmp_path)
    assert len(first.token) >= 40
    assert first.token == second.token
    assert first.origins == ()
    assert (tmp_path / '.env').exists()


def test_explicit_origins_and_reject_wildcard(tmp_path):
    env = tmp_path / '.env'
    env.write_text('GATEWAY_TOKEN=test-token\nALLOWED_ORIGINS=chrome-extension://abc,http://localhost:3210\n', encoding='utf-8')
    assert load_settings(tmp_path).origins == ('chrome-extension://abc', 'http://localhost:3210')
    env.write_text('GATEWAY_TOKEN=test-token\nALLOWED_ORIGINS=*\n', encoding='utf-8')
    with pytest.raises(ValueError, match='explicit'):
        load_settings(tmp_path)
