"""Thin clients: no upstream model imports or live voice cloning."""
from pathlib import Path
import importlib
import httpx
import io
import struct
import wave
import pytest


def wav_bytes():
    stream = io.BytesIO()
    with wave.open(stream, 'wb') as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(24000)
        output.writeframes(b'\x00\x00' * 2400)
    raw = bytearray(stream.getvalue())
    struct.pack_into('<I', raw, 4, 0xFFFFFFFF)
    struct.pack_into('<I', raw, 40, 0xFFFFFFFF)
    return bytes(raw)


class Chunks(httpx.SyncByteStream):
    def __iter__(self):
        raw = wav_bytes()
        yield raw[:13]
        yield raw[13:100]
        yield raw[100:]

ROOT = Path(__file__).resolve().parents[1]


def client_module():
    assert (ROOT / 'cli' / 'speak.py').is_file(), 'CLI client is not implemented'
    return importlib.import_module('cli.speak')


def test_lists_gateway_voices_with_bearer_auth(tmp_path):
    module = client_module()
    def handle(request):
        assert request.url.path == '/v1/voices'
        assert request.headers['authorization'] == 'Bearer fake-test-token'
        return httpx.Response(200, json={'object': 'list', 'data': [
            {'id': 'Mai Anh', 'name': 'Mai Anh', 'kind': 'preset'}]})
    with module.GatewayClient(root=tmp_path, token='fake-test-token',
                              transport=httpx.MockTransport(handle)) as client:
        assert client.list_voices()['data'][0]['name'] == 'Mai Anh'


def test_speak_streams_and_finalizes_unknown_length_wav(tmp_path):
    module = client_module()
    def handle(request):
        import json
        assert request.url.path == '/v1/audio/speech'
        assert json.loads(request.content) == {'input': 'Xin chào', 'voice': 'Mai Anh', 'response_format': 'wav'}
        return httpx.Response(200, stream=Chunks(), headers={'content-type': 'audio/wav'})
    with module.GatewayClient(root=tmp_path, token='test', transport=httpx.MockTransport(handle)) as client:
        result = client.speak('Xin chào')
        second = client.speak('Xin chào')
    path = Path(result['path'])
    assert path.is_absolute() and path.parent == tmp_path / 'outputs'
    assert second['path'] != result['path']
    assert result['sample_rate'] == 24000 and result['duration_seconds'] == 0.1
    assert result['voice'] == 'Mai Anh'
    with wave.open(str(path), 'rb') as output:
        assert output.getnframes() == 2400
    assert struct.unpack_from('<I', path.read_bytes(), 4)[0] == path.stat().st_size - 8


@pytest.mark.parametrize('case', ['upstream', 'env', 'source', 'voices', 'nonwav', 'existing', 'empty', 'long'])
def test_speak_refuses_unsafe_requests_before_http(tmp_path, case):
    module = client_module()
    paths = {'upstream': tmp_path / 'VieNeu-TTS' / 'x.wav',
             'env': tmp_path / '.venv' / 'x.wav',
             'source': tmp_path / 'cli' / 'x.wav',
             'voices': tmp_path / 'voices' / 'x.wav',
             'nonwav': tmp_path / '.env', 'existing': tmp_path / 'existing.wav'}
    if case == 'existing':
        paths[case].write_bytes(b'preserve')
    def handle(_):
        pytest.fail('unsafe request reached gateway')
    text = '' if case == 'empty' else 'x' * 20001 if case == 'long' else 'hello'
    with module.GatewayClient(root=tmp_path, token='test', transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(module.ClientError, match='(?i)(output|text|20000|existing|protected)'):
            client.speak(text, out_path=paths.get(case))
    if case == 'existing':
        assert paths[case].read_bytes() == b'preserve'


@pytest.mark.parametrize('failure', ['offline', 'http', 'invalid', 'truncated'])
def test_failures_are_readable_and_remove_partial_output(tmp_path, failure):
    module = client_module()
    def handle(request):
        if failure == 'offline':
            raise httpx.ConnectError('internal details', request=request)
        if failure == 'http':
            return httpx.Response(401, json={'detail': 'not authorized'})
        raw = b'bad' if failure == 'invalid' else wav_bytes()[:-1]
        return httpx.Response(200, content=raw)
    with module.GatewayClient(root=tmp_path, token='secret-test', transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(module.ClientError) as error:
            client.speak('hello')
    message = str(error.value)
    assert 'secret-test' not in message
    assert ('start start-all.bat' in message) if failure == 'offline' else bool(message)
    assert not list((tmp_path / 'outputs').glob('*.wav'))


def test_clone_upload_has_explicit_consent_and_reference(tmp_path):
    module = client_module()
    clip = tmp_path / 'reference.wav'
    clip.write_bytes(b'fake reference fixture')
    def handle(request):
        assert request.method == 'POST' and request.url.path == '/v1/voices'
        body = request.content.decode()
        for expected in ['name="name"', 'name="file"', 'reference.wav', 'fake reference fixture',
                         'name="denoise"', 'name="description"', 'name="consent_confirmed"',
                         'name="consent_note"', 'owner authorized this test', 'true']:
            assert expected in body
        return httpx.Response(200, json={'name': 'Test clone', 'kind': 'cloned'})
    with module.GatewayClient(root=tmp_path, token='test', transport=httpx.MockTransport(handle)) as client:
        result = client.clone_voice('Test clone', str(clip), True, 'owner authorized this test')
    assert result['kind'] == 'cloned'


@pytest.mark.parametrize('consent,note', [(False, 'owner agrees'), (True, ''), (True, '   ')])
def test_clone_refuses_missing_consent_before_file_or_http(tmp_path, consent, note):
    module = client_module()
    with module.GatewayClient(root=tmp_path, token='test', transport=httpx.MockTransport(lambda _: pytest.fail('HTTP forbidden'))) as client:
        with pytest.raises(module.ClientError, match='(?i)consent'):
            client.clone_voice('Test', 'missing.wav', consent, note)


@pytest.mark.parametrize('kind', ['cloned', 'preset', 'missing'])
def test_delete_only_clones_and_urlencodes_name(tmp_path, kind):
    module = client_module()
    calls = []
    name = 'Test / giọng'
    def handle(request):
        calls.append(request.method)
        if request.method == 'GET':
            return httpx.Response(200, json={'data': [] if kind == 'missing' else [
                {'id': name, 'name': name, 'kind': kind}]})
        assert request.url.raw_path == b'/v1/voices/Test%20%2F%20gi%E1%BB%8Dng'
        return httpx.Response(200, json={'deleted': True, 'name': name})
    with module.GatewayClient(root=tmp_path, token='test', transport=httpx.MockTransport(handle)) as client:
        if kind == 'cloned':
            assert client.delete_voice(name)['deleted'] is True
            assert calls == ['GET', 'DELETE']
        else:
            with pytest.raises(module.ClientError, match='(?i)cloned'):
                client.delete_voice(name)
            assert calls == ['GET']


@pytest.mark.parametrize('mode', ['text', 'file', 'list', 'clone', 'delete'])
def test_cli_modes_and_final_synthesis_path(tmp_path, monkeypatch, capsys, mode):
    module = client_module()
    def handle(request):
        if request.url.path == '/v1/audio/speech':
            return httpx.Response(200, stream=Chunks())
        if request.method == 'GET':
            return httpx.Response(200, json={'data': [{'name': 'Owned', 'kind': 'cloned'}]})
        return httpx.Response(200, json={'name': 'Owned', 'ok': True})
    real = module.GatewayClient
    monkeypatch.setattr(module, 'GatewayClient', lambda: real(root=tmp_path, token='test', transport=httpx.MockTransport(handle)))
    script = tmp_path / 'script.txt'
    script.write_text('Xin chào từ tệp', encoding='utf-8')
    clip = tmp_path / 'reference.wav'
    clip.write_bytes(b'fixture')
    args = {'text': ['Xin chào'], 'file': ['--file', str(script)], 'list': ['--list-voices'],
            'clone': ['--clone', 'Owned', '--ref', str(clip), '--consent'], 'delete': ['--delete-voice', 'Owned']}[mode]
    assert module.main(args) == 0
    captured = capsys.readouterr()
    assert not captured.err
    if mode in ('text', 'file'):
        assert Path(captured.out.strip().splitlines()[-1]).is_file()
    else:
        import json
        assert json.loads(captured.out)


def test_cli_error_has_no_traceback(tmp_path, monkeypatch, capsys):
    module = client_module()
    def handle(request):
        raise httpx.ConnectError('internal', request=request)
    real = module.GatewayClient
    monkeypatch.setattr(module, 'GatewayClient', lambda: real(root=tmp_path, token='test', transport=httpx.MockTransport(handle)))
    assert module.main(['hello']) == 1
    captured = capsys.readouterr()
    assert 'start start-all.bat' in captured.err
    assert 'Traceback' not in captured.err


@pytest.mark.parametrize('args', [[], ['text', '--list-voices'], ['--clone', 'Test', '--ref', 'missing.wav'],
                                  ['--clone', 'Test', '--consent'], ['text', '--consent']])
def test_cli_refuses_ambiguous_or_unconsented_modes(args, monkeypatch, capsys):
    module = client_module()
    monkeypatch.setattr(module, 'GatewayClient', lambda: pytest.fail('must validate before HTTP'))
    with pytest.raises(SystemExit) as error:
        module.main(args)
    assert error.value.code == 2
    assert capsys.readouterr().err


def test_dotenv_loading_and_missing_token_are_safe(tmp_path, monkeypatch):
    module = client_module()
    monkeypatch.delenv('GATEWAY_TOKEN', raising=False)
    monkeypatch.delenv('GATEWAY_URL', raising=False)
    with pytest.raises(module.ClientError, match='GATEWAY_TOKEN'):
        module.GatewayClient(root=tmp_path)
    (tmp_path / '.env').write_text('GATEWAY_TOKEN=fake-local-only\nGATEWAY_URL=http://test.local:8123\n', encoding='utf-8')
    def handle(request):
        assert request.url.host == 'test.local' and request.url.port == 8123
        assert request.headers['Authorization'] == 'Bearer fake-local-only'
        return httpx.Response(200, json={'data': []})
    with module.GatewayClient(root=tmp_path, transport=httpx.MockTransport(handle)) as client:
        assert client.list_voices() == {'data': []}


def test_explicit_external_wav_is_allowed(tmp_path):
    module = client_module()
    root = tmp_path / 'project'
    output = tmp_path / 'external' / 'new.wav'
    with module.GatewayClient(root=root, token='test', transport=httpx.MockTransport(
            lambda _: httpx.Response(200, stream=Chunks()))) as client:
        assert client.speak('hello', out_path=output)['path'] == str(output)
    assert output.is_file()


@pytest.mark.parametrize('folder', ['.venv', 'VieNeu-TTS', 'voices'])
def test_explicit_external_output_cannot_target_environment_or_voice_files(tmp_path, folder):
    module = client_module()
    with module.GatewayClient(root=tmp_path / 'project', token='test', transport=httpx.MockTransport(
            lambda _: pytest.fail('protected external output reached HTTP'))) as client:
        with pytest.raises(module.ClientError, match='(?i)protected'):
            client.speak('hello', out_path=tmp_path / folder / 'new.wav')


def test_default_output_junction_cannot_write_upstream(tmp_path):
    import subprocess
    module = client_module()
    upstream = tmp_path / 'VieNeu-TTS'
    upstream.mkdir()
    result = subprocess.run(['cmd.exe', '/c', 'mklink', '/J', str(tmp_path / 'outputs'), str(upstream)], capture_output=True)
    assert result.returncode == 0, 'test junction setup failed'
    try:
        with module.GatewayClient(root=tmp_path, token='test', transport=httpx.MockTransport(
                lambda _: pytest.fail('protected junction reached HTTP'))) as client:
            with pytest.raises(module.ClientError, match='(?i)protected'):
                client.speak('hello')
        assert not list(upstream.iterdir())
    finally:
        (tmp_path / 'outputs').rmdir()
