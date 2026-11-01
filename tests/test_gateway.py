from pathlib import Path
import io
import json
import wave
import pytest


def synthetic_wav():
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as w:
        w.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
        w.writeframes(b'\0\0' * 48000)
    return buf.getvalue()

import httpx
from fastapi.testclient import TestClient

from gateway.app import create_app
from gateway.config import Settings


def fake_upstream(request):
    if request.url.path == '/health':
        return httpx.Response(200, json={'status': 'ok', 'backend': 'pytorch'})
    if request.url.path == '/v1/voices':
        return httpx.Response(200, json={'object': 'list', 'data': [{'id': 'Mai Anh', 'name': 'Mai Anh'}]})
    return httpx.Response(200, json={'object': 'list', 'data': []})


def test_health_public_but_all_other_routes_need_token(tmp_path):
    app = create_app(Settings(root=tmp_path, token='test-only'), transport=httpx.MockTransport(fake_upstream))
    with TestClient(app) as client:
        assert client.get('/health').status_code == 200
        assert client.get('/v1/models').status_code == 401
        assert client.get('/v1/models', headers={'Authorization': 'Bearer wrong'}).status_code == 401
        assert client.get('/v1/models', headers={'Authorization': 'Bearer test-only'}).status_code == 200
        assert client.get('/missing').status_code == 401
        assert client.get('/openapi.json').status_code == 401


def test_cors_explicit_allow_deny_no_wildcard(tmp_path):
    app = create_app(Settings(root=tmp_path, token='test-only', origins=('chrome-extension://testid', 'http://localhost:3210')), transport=httpx.MockTransport(fake_upstream))
    with TestClient(app) as client:
        for origin in ('chrome-extension://testid', 'http://localhost:3210'):
            r = client.options('/v1/audio/speech', headers={'Origin': origin, 'Access-Control-Request-Method': 'POST', 'Access-Control-Request-Headers': 'authorization,content-type'})
            assert r.status_code == 200
            assert r.headers['access-control-allow-origin'] == origin
        r = client.options('/v1/audio/speech', headers={'Origin': 'https://evil.example', 'Access-Control-Request-Method': 'POST'})
        assert r.status_code == 400
        assert 'access-control-allow-origin' not in r.headers
        r = client.get('/v1/models', headers={'Origin': 'https://evil.example', 'Authorization': 'Bearer test-only'})
        assert r.status_code == 403
        r = client.get('/v1/models', headers={'Origin': 'http://localhost:3210'})
        assert r.status_code == 401
        assert r.headers['access-control-allow-origin'] == 'http://localhost:3210'


def test_clone_persists_and_replays_after_upstream_and_gateway_restart(tmp_path):
    enrollments = []
    ram = {}
    def upstream(req):
        if req.url.path == '/v1/voices' and req.method == 'POST':
            content = req.read()
            assert b'RIFF' in content
            enrollments.append(content)
            ram['Synthetic Test'] = True
            return httpx.Response(200, json={'id': 'Synthetic Test', 'name': 'Synthetic Test'})
        return fake_upstream(req)
    settings = Settings(root=tmp_path, token='test-only')
    headers = {'Authorization': 'Bearer test-only'}
    app = create_app(settings, transport=httpx.MockTransport(upstream))
    with TestClient(app) as client:
        r = client.post('/v1/voices', headers=headers, data={'name': 'Synthetic Test', 'consent_confirmed': 'true', 'consent_note': 'Generated silent test signal, no human speaker'}, files={'file': ('synthetic.wav', synthetic_wav(), 'audio/wav')})
        assert r.status_code == 200, r.text
        metadata_files = list((tmp_path / 'voices').glob('*.json'))
        assert len(metadata_files) == 1
        meta = json.loads(metadata_files[0].read_text(encoding='utf-8'))
        assert meta['name'] == 'Synthetic Test'
        assert meta['created_at'] and 'no human' in meta['consent_note']
        with wave.open(str(metadata_files[0].with_suffix('.wav')), 'rb') as w:
            assert w.getnframes() > 0
    ram.clear()
    with TestClient(create_app(settings, transport=httpx.MockTransport(upstream))) as client:
        assert ram == {'Synthetic Test': True}
        assert len(enrollments) == 2
        voices = client.get('/v1/voices', headers=headers).json()['data']
        assert any(v['name'] == 'Synthetic Test' and v['kind'] == 'cloned' for v in voices)
        r = client.delete('/v1/voices/Synthetic%20Test', headers=headers)
        assert r.status_code == 200
        assert r.json()['upstream_removed'] is False
        assert client.delete('/v1/voices/Synthetic%20Test', headers=headers).status_code == 200
        assert client.delete('/v1/voices/!invalid', headers=headers).status_code == 400
        assert not list((tmp_path / 'voices').glob('*.wav'))
        assert not list((tmp_path / 'voices').glob('*.json'))
        assert client.delete('/v1/voices/Mai%20Anh', headers=headers).status_code == 409
        r = client.post('/v1/audio/speech', headers=headers, json={'input': 'test', 'voice': 'Synthetic Test'})
        assert r.status_code == 400
    with TestClient(create_app(settings, transport=httpx.MockTransport(upstream))):
        assert len(enrollments) == 2


def test_proxy_reports_upstream_down_without_exposing_request_secrets(tmp_path):
    down = False
    def upstream(req):
        if down:
            raise httpx.ConnectError('internal address details')
        return fake_upstream(req)
    with TestClient(create_app(Settings(root=tmp_path, token='test-only'), transport=httpx.MockTransport(upstream))) as client:
        down = True
        r = client.get('/v1/models', headers={'Authorization': 'Bearer test-only'})
        assert r.status_code == 503
        assert 'start start-all.bat' in r.text
        assert 'internal address' not in r.text


def test_clone_requires_consent_and_blocks_presets(tmp_path):
    with TestClient(create_app(Settings(root=tmp_path, token='test-only'), transport=httpx.MockTransport(fake_upstream))) as client:
        headers = {'Authorization': 'Bearer test-only'}
        r = client.post('/v1/voices', headers=headers, data={'name': 'Synthetic'}, files={'file': ('test.wav', synthetic_wav())})
        assert r.status_code == 403
        r = client.post('/v1/voices', headers=headers, data={'name': 'Mai Anh', 'consent_confirmed': 'true', 'consent_note': 'not real'}, files={'file': ('test.wav', synthetic_wav())})
        assert r.status_code == 409
