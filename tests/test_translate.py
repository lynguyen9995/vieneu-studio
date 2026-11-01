"""/v1/translate (Ollama hy-mt2, mocked) and /v1/pair (token for the listed extension only)."""
import json

import httpx
from fastapi.testclient import TestClient

from gateway.app import create_app
from gateway.config import Settings
from gateway.translate import Translator, loopback_url, suspicious
import pytest

AUTH = {'Authorization': 'Bearer test-only'}
EXT = 'chrome-extension://abcdefghijklmnopabcdefghijklmnop'


def upstream(request):
    if request.url.path == '/health':
        return httpx.Response(200, json={'status': 'ok', 'backend': 'pytorch'})
    if request.url.path == '/v1/voices':
        return httpx.Response(200, json={'object': 'list', 'data': [{'id': 'Mai Anh', 'name': 'Mai Anh'}]})
    return httpx.Response(200, json={})


def fake_ollama(calls, broken_groups=False, dup_groups=False):
    def handler(request):
        if request.url.path == '/api/tags':
            return httpx.Response(200, json={'models': [{'name': 'hy-mt2:latest'}]})
        body = json.loads(request.content)
        prompt = body['messages'][0]['content']
        calls.append(prompt)
        text = prompt.split('\n\n', 1)[1]
        lines = text.split('\n')
        if broken_groups and len(lines) > 1:
            return httpx.Response(200, json={'message': {'content': 'một dòng gộp duy nhất'}})
        if dup_groups and len(lines) > 1:            # merged lines 1+2, then repeated one to keep the count
            vi = [f'VI({lines[0]} {lines[1]})', f'VI({lines[0]} {lines[1]})'] + [f'VI({l})' for l in lines[2:]]
            return httpx.Response(200, json={'message': {'content': '\n'.join(vi)}})
        return httpx.Response(200, json={'message': {'content': '\n'.join(f'VI({l})' for l in lines)}})
    return handler


def make(tmp_path, calls, **kw):
    s = Settings(root=tmp_path, token='test-only', origins=(EXT, 'http://localhost:3210'))
    return create_app(s, transport=httpx.MockTransport(upstream),
                      translate_transport=httpx.MockTransport(fake_ollama(calls, **kw)))


def test_translate_groups_keep_order_and_blanks(tmp_path):
    calls = []
    with TestClient(make(tmp_path, calls)) as c:
        texts = ['Hello', '', 'How are you?', 'Fine', 'Bye', 'See you', 'Later', 'Again']
        r = c.post('/v1/translate', json={'texts': texts, 'group': 4}, headers=AUTH)
        assert r.status_code == 200, r.text
        out = r.json()['translations']
        assert out == ['VI(Hello)', '', 'VI(How are you?)', 'VI(Fine)', 'VI(Bye)', 'VI(See you)', 'VI(Later)', 'VI(Again)']
        assert len(calls) == 2 and 'into Vietnamese' in calls[0] and '3 lines' in calls[0]


def test_translate_falls_back_line_by_line(tmp_path):
    calls = []
    with TestClient(make(tmp_path, calls, broken_groups=True)) as c:
        r = c.post('/v1/translate', json={'texts': ['A', 'B', 'C']}, headers=AUTH)
        assert r.json()['translations'] == ['VI(A)', 'VI(B)', 'VI(C)']
        assert len(calls) == 4          # 1 group attempt + 3 single lines


def test_translate_validation_and_auth(tmp_path):
    with TestClient(make(tmp_path, [])) as c:
        assert c.post('/v1/translate', json={'texts': ['x']}).status_code == 401
        assert c.post('/v1/translate', json={'texts': []}, headers=AUTH).status_code == 400
        assert c.post('/v1/translate', json={'texts': [1]}, headers=AUTH).status_code == 400
        assert c.post('/v1/translate', json={'texts': ['x' * 2001]}, headers=AUTH).status_code == 413
        assert c.get('/v1/translate/status', headers=AUTH).json() == {'ok': True, 'model': 'hy-mt2'}


def test_translate_reports_ollama_down(tmp_path):
    def down(request):
        raise httpx.ConnectError('refused')
    s = Settings(root=tmp_path, token='test-only')
    with TestClient(create_app(s, transport=httpx.MockTransport(upstream), translate_transport=httpx.MockTransport(down))) as c:
        r = c.post('/v1/translate', json={'texts': ['a', 'b']}, headers=AUTH)
        assert r.status_code == 503 and 'Ollama' in r.json()['detail']
        assert c.get('/v1/translate/status', headers=AUTH).json()['ok'] is False


def test_pair_only_for_listed_extension(tmp_path):
    with TestClient(make(tmp_path, [])) as c:
        r = c.post('/v1/pair', headers={'Origin': EXT})
        assert r.status_code == 200 and r.json()['token'] == 'test-only'
        assert r.headers['access-control-allow-origin'] == EXT
        assert c.post('/v1/pair').status_code == 403                                           # no origin
        assert c.post('/v1/pair', headers={'Origin': 'http://localhost:3210'}).status_code == 403  # web origin
        assert c.post('/v1/pair', headers={'Origin': 'chrome-extension://otherotherotherotherotherother'}).status_code == 403
        assert c.post('/v1/pair', headers={'Origin': 'https://evil.example'}).status_code == 403


def test_ollama_url_must_be_loopback():
    assert loopback_url('http://127.0.0.1:11434/') == 'http://127.0.0.1:11434'
    with pytest.raises(ValueError):
        Translator('http://evil.example:11434', 'hy-mt2')


def test_translate_repeated_group_line_falls_back(tmp_path):
    calls = []
    with TestClient(make(tmp_path, calls, dup_groups=True)) as c:
        r = c.post('/v1/translate', json={'texts': ['Good morning', 'We start now', 'Look here']}, headers=AUTH)
        assert r.json()['translations'] == ['VI(Good morning)', 'VI(We start now)', 'VI(Look here)']
        assert len(calls) == 4                      # 1 group + 3 single lines


def test_suspicious_rules():
    assert not suspicious(['Hello', 'Bye'], ['Xin chào', 'Tạm biệt'])
    assert suspicious(['Hello', 'Bye'], ['Xin chào', 'Xin chào!'])          # repeated line
    assert not suspicious(['Yes', 'Yes'], ['Vâng', 'Vâng'])                  # source repeats too: fine
    assert suspicious(['Hi', 'Ok'], ['Xin chào, hôm nay chúng ta sẽ nói về rất nhiều thứ khác nhau', 'Ừ'])
    assert suspicious(['This is a fairly long English sentence about cooking rice', 'Ok'], ['Cơm', 'Ừ'])
