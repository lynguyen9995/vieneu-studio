"""Same-origin web studio: public static page only on loopback hosts, token-protected APIs."""
import io
import wave

import httpx
from fastapi.testclient import TestClient

from gateway.app import create_app
from gateway.config import Settings

HOSTS = frozenset({'testserver', '127.0.0.1:8001'})
AUTH = {'Authorization': 'Bearer test-only'}


def fake_upstream(request):
    if request.url.path == '/health':
        return httpx.Response(200, json={'status': 'ok', 'backend': 'pytorch'})
    if request.url.path == '/v1/voices':
        return httpx.Response(200, json={'object': 'list', 'data': [{'id': 'Mai Anh', 'name': 'Mai Anh'}]})
    return httpx.Response(200, json={'object': 'list', 'data': []})


def wav_bytes(seconds=0.5, rate=48000):
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as w:
        w.setparams((1, 2, rate, 0, 'NONE', 'not compressed'))
        w.writeframes(b'\0\0' * int(rate * seconds))
    return buf.getvalue()


def make(tmp_path, **kw):
    (tmp_path / 'webapp').mkdir()
    (tmp_path / 'webapp' / 'index.html').write_text('<meta name="vieneu-token" content="__VIENEU_TOKEN__">', encoding='utf-8')
    (tmp_path / 'webapp' / 'app.js').write_text('console.log(1)', encoding='utf-8')
    (tmp_path / 'webapp' / 'style.css').write_text('body{}', encoding='utf-8')
    settings = Settings(root=tmp_path, token='test-only', **kw)
    return create_app(settings, transport=httpx.MockTransport(fake_upstream), web_hosts=HOSTS)


def test_static_page_public_with_token_and_security_headers(tmp_path):
    with TestClient(make(tmp_path)) as c:
        r = c.get('/app/')
        assert r.status_code == 200 and 'content="test-only"' in r.text
        assert "frame-ancestors 'none'" in r.headers['content-security-policy']
        assert r.headers['cache-control'] == 'no-store'
        assert c.get('/app/app.js').status_code == 200
        assert c.get('/app/style.css').status_code == 200
        assert c.get('/app', follow_redirects=False).status_code == 307
        assert c.get('/app/secret.txt').status_code == 401   # unknown static names are not public


def test_foreign_host_cannot_load_page(tmp_path):
    """DNS-rebinding guard: the token page is served only for loopback Host headers."""
    with TestClient(make(tmp_path)) as c:
        r = c.get('/app/', headers={'Host': 'evil.example:8001'})
        assert r.status_code == 403 and 'test-only' not in r.text


def test_api_routes_need_token_and_rest_unchanged(tmp_path):
    with TestClient(make(tmp_path)) as c:
        assert c.get('/app/api/outputs').status_code == 401
        assert c.get('/app/api/outputs', headers=AUTH).status_code == 200
        assert c.get('/v1/models').status_code == 401
        assert c.get('/missing').status_code == 401


def test_self_origin_allowed_foreign_origin_denied(tmp_path):
    with TestClient(make(tmp_path)) as c:
        assert c.get('/v1/voices', headers={**AUTH, 'Origin': 'http://127.0.0.1:8001'}).status_code == 200
        assert c.get('/v1/voices', headers={**AUTH, 'Origin': 'https://evil.example'}).status_code == 403


def test_save_list_and_fetch_outputs(tmp_path):
    with TestClient(make(tmp_path)) as c:
        r = c.post('/app/api/outputs?label=Hội thoại 1', content=wav_bytes(), headers={**AUTH, 'Content-Type': 'audio/wav'})
        assert r.status_code == 200, r.text
        name = r.json()['name']
        assert '-web-' in name and name.endswith('.wav') and '/' not in name
        assert r.json()['sample_rate'] == 48000
        items = c.get('/app/api/outputs', headers=AUTH).json()['data']
        assert items[0]['name'] == name and items[0]['source'] == 'web' and items[0]['duration_seconds'] == 0.5
        got = c.get('/app/api/outputs/' + name, headers=AUTH)
        assert got.status_code == 200 and got.content == wav_bytes()


def test_outputs_reject_bad_bodies_and_traversal(tmp_path):
    with TestClient(make(tmp_path)) as c:
        assert c.post('/app/api/outputs', content=b'not a wav', headers=AUTH).status_code == 400
        assert c.get('/app/api/outputs/..%2F.env', headers=AUTH).status_code in (400, 404)
        assert c.get('/app/api/outputs/missing.wav', headers=AUTH).status_code == 404
        assert c.get('/app/api/outputs/a%20b.wav', headers=AUTH).status_code == 400


# ---------- step 3b: model switch, clone preview, samples ----------
import time
import types

from gateway import engine as real_engine


def rich_upstream(calls):
    def handler(request):
        calls.append((request.method, request.url.path))
        if request.url.path == '/health':
            return httpx.Response(200, json={'status': 'ok', 'backend': 'pytorch', 'max_streams': 4})
        if request.url.path == '/v1/voices' and request.method == 'GET':
            return httpx.Response(200, json={'object': 'list', 'data': [{'id': 'Mai Anh', 'name': 'Mai Anh'}, {'id': 'Ái Hân', 'name': 'Ái Hân'}]})
        if request.url.path == '/v1/voices' and request.method == 'POST':
            return httpx.Response(200, json={'id': 'studio-preview'})
        if request.url.path == '/v1/audio/speech':
            return httpx.Response(200, content=b'\x01\x00' * 480, headers={'x-sample-rate': '48000'})
        return httpx.Response(200, json={'object': 'list', 'data': []})
    return handler


def fake_ctl(events):
    pids = iter([4321, None, None, None])
    proc = types.SimpleNamespace(poll=lambda: None)
    return types.SimpleNamespace(
        OPTIONS=real_engine.OPTIONS, ENGINE_DIR=real_engine.ENGINE_DIR, load_config=real_engine.load_config,
        validate=real_engine.validate, save_config=real_engine.save_config, log_tail=lambda root: '',
        listener_pid=lambda: next(pids, None), kill_pid=lambda pid: events.append(('kill', pid)),
        spawn=lambda root, cfg: events.append(('spawn', cfg['preset'])) or proc)


def make_rich(tmp_path, calls, events):
    (tmp_path / 'webapp').mkdir()
    for n, body in (('index.html', '<meta content="__VIENEU_TOKEN__">'), ('app.js', ''), ('style.css', '')):
        (tmp_path / 'webapp' / n).write_text(body, encoding='utf-8')
    return create_app(Settings(root=tmp_path, token='test-only'), transport=httpx.MockTransport(rich_upstream(calls)),
                      web_hosts=HOSTS, engine_ctl=fake_ctl(events))


def test_engine_options_and_validation(tmp_path):
    with TestClient(make_rich(tmp_path, [], [])) as c:
        j = c.get('/app/api/engine', headers=AUTH).json()
        assert j['current']['preset'] == 'turbo-gpu' and j['current']['state'] == 'ready'
        assert {o['id'] for o in j['options']} == {'turbo-gpu', 'turbo-cpu', 'turbo-int8', 'nano', 'lora'}
        assert c.post('/app/api/engine', json={'preset': 'v2'}, headers=AUTH).status_code == 400
        assert c.post('/app/api/engine', json={'preset': 'lora', 'lora_path': ''}, headers=AUTH).status_code == 400
        assert c.post('/app/api/engine', json={'preset': 'lora', 'lora_path': str(tmp_path / 'nope')}, headers=AUTH).status_code == 400
        assert c.get('/app/api/engine').status_code == 401
        assert c.get('/health').json()['engine']['label'] == 'v3 Turbo · GPU'


def test_engine_switch_restarts_and_resyncs(tmp_path):
    calls, events = [], []
    with TestClient(make_rich(tmp_path, calls, events)) as c:
        r = c.post('/app/api/engine', json={'preset': 'nano'}, headers=AUTH)
        assert r.status_code == 200 and r.json()['state'] == 'loading'
        for _ in range(100):
            cur = c.get('/app/api/engine', headers=AUTH).json()['current']
            if cur['state'] != 'loading':
                break
            time.sleep(0.05)
        assert cur['state'] == 'ready', cur
        assert cur['preset'] == 'nano' and ('kill', 4321) in events and ('spawn', 'nano') in events
        assert (tmp_path / 'engine.json').exists()
        names = [v['id'] for v in c.get('/v1/voices', headers=AUTH).json()['data']]
        assert 'Ái Hân' in names          # presets re-snapshotted from the new engine


def test_clone_preview_returns_audio_without_saving(tmp_path):
    calls = []
    with TestClient(make_rich(tmp_path, calls, [])) as c:
        r = c.post('/app/api/clone-preview', headers=AUTH, data={'text': 'Xin chào', 'denoise': 'true'},
                   files={'file': ('clip.wav', wav_bytes(1.0), 'audio/wav')})
        assert r.status_code == 200 and r.headers['x-sample-rate'] == '48000' and len(r.content) == 960
        assert ('POST', '/v1/voices') in calls and ('POST', '/v1/audio/speech') in calls
        assert not list((tmp_path / 'voices').glob('*.json'))           # nothing persisted
        assert c.post('/app/api/clone-preview', headers=AUTH, data={'text': ''},
                      files={'file': ('clip.wav', wav_bytes(), 'audio/wav')}).status_code in (400, 422)


def test_preview_name_reserved_and_not_speakable(tmp_path):
    with TestClient(make_rich(tmp_path, [], [])) as c:
        r = c.post('/v1/voices', headers=AUTH, data={'name': 'studio-preview', 'consent_confirmed': 'true', 'consent_note': 'mine'},
                   files={'file': ('clip.wav', wav_bytes(), 'audio/wav')})
        assert r.status_code == 409
        assert c.post('/v1/audio/speech', headers=AUTH, json={'input': 'x', 'voice': 'studio-preview'}).status_code == 400


def test_samples_listed_and_served(tmp_path):
    ref = tmp_path / 'VieNeu-TTS' / 'examples' / 'audio_ref'
    ref.mkdir(parents=True)
    (ref / 'example.wav').write_bytes(wav_bytes())
    (ref / 'example.txt').write_text('Câu mẫu.', encoding='utf-8')
    with TestClient(make_rich(tmp_path, [], [])) as c:
        items = c.get('/app/api/samples', headers=AUTH).json()['data']
        assert items == [{'name': 'example.wav', 'text': 'Câu mẫu.'}]
        assert c.get('/app/api/samples/example.wav', headers=AUTH).content == wav_bytes()
        assert c.get('/app/api/samples/..%2Fx.wav', headers=AUTH).status_code == 404
