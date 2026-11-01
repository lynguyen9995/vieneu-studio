"""Gateway → speech-to-text service (8002): token, id validation, passthrough, OpenAI transcriptions, errors."""
import json

import httpx
from fastapi.testclient import TestClient

from gateway.app import create_app
from gateway.config import Settings
from gateway.launch import service_state
import pytest

AUTH = {'Authorization': 'Bearer test-only'}
EXT = 'chrome-extension://abcdefghijklmnopabcdefghijklmnop'
VID = 'dQw4w9WgXcQ'


def upstream(request):
    if request.url.path == '/health':
        return httpx.Response(200, json={'status': 'ok', 'backend': 'pytorch'})
    if request.url.path == '/v1/voices':
        return httpx.Response(200, json={'object': 'list', 'data': [{'id': 'Mai Anh', 'name': 'Mai Anh'}]})
    return httpx.Response(200, json={})


def fake_stt(calls):
    def handler(request):
        calls.append(request)
        path = request.url.path
        if path == '/health':
            return httpx.Response(200, json={'status': 'ok', 'service': 'vieneu-stt', 'loaded': False})
        if path == '/prepare':
            return httpx.Response(200, json={'video_id': json.loads(request.content)['video_id'], 'state': 'queued'})
        if path.startswith('/jobs/'):
            return httpx.Response(200, json={'state': 'transcribing', 'since': request.url.params.get('since'),
                                             'segments': [{'start': 1.0, 'end': 2.0, 'text': 'Hi'}], 'next': 3})
        if path == '/transcribe':
            return httpx.Response(200, json={'language': request.url.params.get('language') or 'en', 'duration': 2.0,
                                             'segments': [{'start': 0.0, 'end': 2.0, 'text': 'Hello there'}],
                                             'text': 'Hello there'})
        return httpx.Response(404, json={})
    return handler


def make(tmp_path, calls, stt_handler=None):
    s = Settings(root=tmp_path, token='test-only', origins=(EXT,))
    return create_app(s, transport=httpx.MockTransport(upstream),
                      stt_transport=httpx.MockTransport(stt_handler or fake_stt(calls)))


def test_listen_needs_token_and_valid_video_id(tmp_path):
    calls = []
    with TestClient(make(tmp_path, calls)) as c:
        assert c.post('/v1/listen/prepare', json={'video_id': VID}).status_code == 401
        for bad in ['short', 'https://youtu.be/x', '../../../etc', 123]:
            assert c.post('/v1/listen/prepare', json={'video_id': bad}, headers=AUTH).status_code == 400
        assert c.get('/v1/listen/jobs/not-an-id', headers=AUTH).status_code == 400
        assert calls == []                                   # nothing bad reached the service


def test_prepare_and_poll_are_passed_through(tmp_path):
    calls = []
    with TestClient(make(tmp_path, calls)) as c:
        r = c.post('/v1/listen/prepare', json={'video_id': VID}, headers=AUTH)
        assert r.status_code == 200 and r.json() == {'video_id': VID, 'state': 'queued'}
        j = c.get(f'/v1/listen/jobs/{VID}', params={'since': 2}, headers=AUTH).json()
        assert j['since'] == '2' and j['segments'][0]['text'] == 'Hi'
        assert c.get('/v1/listen/status', headers=AUTH).json()['service'] == 'vieneu-stt'


def test_openai_style_transcriptions(tmp_path):
    calls = []
    with TestClient(make(tmp_path, calls)) as c:
        files = {'file': ('chunk.wav', b'RIFF....WAVEfmt ', 'audio/wav')}
        assert c.post('/v1/audio/transcriptions', files=files, headers=AUTH).json() == {'text': 'Hello there'}
        v = c.post('/v1/audio/transcriptions', files=files, data={'language': 'ko', 'response_format': 'verbose_json'},
                   headers=AUTH).json()
        assert v['language'] == 'ko' and v['segments'][0]['end'] == 2.0 and v['task'] == 'transcribe'
        t = c.post('/v1/audio/transcriptions', files=files, data={'response_format': 'text'}, headers=AUTH)
        assert t.text == 'Hello there' and t.headers['content-type'].startswith('text/plain')
        sent = [x for x in calls if x.url.path == '/transcribe'][0]
        assert sent.content == b'RIFF....WAVEfmt '


def test_stt_down_gives_readable_503(tmp_path):
    def down(request):
        raise httpx.ConnectError('refused')
    with TestClient(make(tmp_path, [], stt_handler=down)) as c:
        r = c.post('/v1/listen/prepare', json={'video_id': VID}, headers=AUTH)
        assert r.status_code == 503 and 'Whisper' in r.json()['detail']


def test_extension_origin_can_call_listen(tmp_path):
    with TestClient(make(tmp_path, [])) as c:
        pre = c.options('/v1/listen/prepare', headers={'Origin': EXT, 'Access-Control-Request-Method': 'POST',
                                                       'Access-Control-Request-Headers': 'authorization,content-type'})
        assert pre.status_code == 200 and pre.headers['access-control-allow-origin'] == EXT
        r = c.post('/v1/listen/prepare', json={'video_id': VID}, headers={**AUTH, 'Origin': EXT})
        assert r.status_code == 200
        assert c.post('/v1/listen/prepare', json={'video_id': VID},
                      headers={**AUTH, 'Origin': 'https://evil.example'}).status_code == 403


def test_launcher_recognizes_only_our_stt_service():
    ok = httpx.MockTransport(lambda r: httpx.Response(200, json={'status': 'ok', 'service': 'vieneu-stt'}))
    with httpx.Client(transport=ok) as client:
        assert service_state(client, 8002) == 'ready'
    other = httpx.MockTransport(lambda r: httpx.Response(200, json={'status': 'ok', 'service': 'something-else'}))
    with httpx.Client(transport=other) as client:
        with pytest.raises(RuntimeError, match='unrecognized'):
            service_state(client, 8002)
