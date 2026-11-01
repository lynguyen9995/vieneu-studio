"""Verify the official MCP SDK registration and stdio protocol."""
from pathlib import Path
import importlib.util
import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_server():
    path = ROOT / 'mcp' / 'server.py'
    assert path.is_file(), 'Official SDK MCP server is not implemented'
    spec = importlib.util.spec_from_file_location('vieneu_mcp_server_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_official_sdk_registers_exact_four_typed_tools():
    module = load_server()
    from mcp.server.fastmcp import FastMCP
    assert isinstance(module.mcp, FastMCP)
    tools = {tool.name: tool for tool in await module.mcp.list_tools()}
    assert set(tools) == {'speak', 'list_voices', 'clone_voice', 'delete_voice'}
    speak = tools['speak'].inputSchema
    assert speak['required'] == ['text']
    assert speak['properties']['voice']['default'] == 'Mai Anh'
    clone = tools['clone_voice'].inputSchema
    assert set(clone['required']) == {'name', 'audio_path', 'consent_confirmed', 'consent_note'}
    assert clone['properties']['consent_confirmed']['type'] == 'boolean'
    assert 'owner' in tools['clone_voice'].description.lower()
    assert 'agree' in tools['clone_voice'].description.lower()
    assert 'out_path' in tools['speak'].description
    assert 'absolute' in tools['clone_voice'].description.lower()
    assert 'consent_note' in tools['clone_voice'].description
    assert 'name' in tools['delete_voice'].description
    assert not (ROOT / 'mcp' / '__init__.py').exists()


@pytest.mark.asyncio
async def test_mcp_tools_use_real_http_client_and_return_structured_results(tmp_path, monkeypatch):
    import httpx
    import io
    import wave
    module = load_server()
    calls = []
    data = io.BytesIO()
    with wave.open(data, 'wb') as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(24000)
        audio.writeframes(b'\x00\x00' * 240)
    clip = tmp_path / 'ref.wav'
    clip.write_bytes(data.getvalue())
    def handle(request):
        calls.append((request.method, request.url.path))
        if request.url.path == '/v1/audio/speech':
            return httpx.Response(200, content=data.getvalue())
        if request.method == 'GET':
            return httpx.Response(200, json={'data': [{'name': 'Test', 'kind': 'cloned'}]})
        return httpx.Response(200, json={'name': 'Test', 'ok': True})
    real = module.GatewayClient
    monkeypatch.setattr(module, 'GatewayClient', lambda: real(root=tmp_path, token='fake', transport=httpx.MockTransport(handle)))
    for name, args in [('speak', {'text': 'hello'}), ('list_voices', {}),
                       ('clone_voice', {'name': 'Test', 'audio_path': str(clip), 'consent_confirmed': True, 'consent_note': 'owner agreed'}),
                       ('delete_voice', {'name': 'Test'})]:
        content, structured = await module.mcp.call_tool(name, args)
        assert isinstance(structured, dict)
        assert 'error' not in structured
        assert all(item.type == 'text' for item in content)
        if name == 'speak':
            assert Path(structured['path']).is_file()
            assert structured['duration_seconds'] == 0.01
    assert ('POST', '/v1/voices') in calls and ('DELETE', '/v1/voices/Test') in calls


@pytest.mark.asyncio
@pytest.mark.parametrize('consent,note', [(False, 'owner agrees'), (True, '   ')])
async def test_mcp_refuses_clone_consent_without_creating_client(monkeypatch, consent, note):
    module = load_server()
    monkeypatch.setattr(module, 'GatewayClient', lambda: pytest.fail('client must not be opened'))
    _, result = await module.mcp.call_tool('clone_voice', {'name': 'Test', 'audio_path': 'missing.wav',
                                                         'consent_confirmed': consent, 'consent_note': note})
    assert result['ok'] is False
    assert 'consent' in result['error'].lower()


@pytest.mark.asyncio
async def test_mcp_unavailable_returns_readable_error(monkeypatch):
    module = load_server()
    def unavailable():
        raise module.ClientError('Gateway unavailable; start start-all.bat')
    monkeypatch.setattr(module, 'GatewayClient', unavailable)
    _, result = await module.mcp.call_tool('speak', {'text': 'hello'})
    assert result == {'ok': False, 'error': 'Gateway unavailable; start start-all.bat'}


@pytest.mark.asyncio
@pytest.mark.parametrize('launcher', ['python', 'uv'])
async def test_real_stdio_initialization_and_tool_listing(tmp_path, launcher):
    import sys
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    command = sys.executable if launcher == 'python' else 'C:/Users/Phuoc Ly/.local/bin/uv.exe'
    args = [str(ROOT / 'mcp' / 'server.py')]
    if launcher == 'uv':
        args = ['--directory', str(ROOT), 'run', '--no-sync', 'python', *args]
    parameters = StdioServerParameters(command=command, args=args, cwd=str(tmp_path))
    async with stdio_client(parameters) as (read, write):
        async with ClientSession(read, write, read_timeout_seconds=__import__('datetime').timedelta(seconds=15)) as session:
            initialized = await session.initialize()
            assert initialized.serverInfo.name == 'VieNeu Gateway'
            tools = await session.list_tools()
            assert {tool.name for tool in tools.tools} == {'speak', 'list_voices', 'clone_voice', 'delete_voice'}
            result = await session.call_tool('clone_voice', {'name': 'Test', 'audio_path': 'missing.wav',
                                                            'consent_confirmed': False, 'consent_note': 'no permission'})
            assert result.structuredContent['ok'] is False
            assert 'consent' in result.structuredContent['error'].lower()
