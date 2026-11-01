"""Non-destructive verification; no enrollment/deletion and no secret output."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import wave

ROOT = Path(__file__).resolve().parent

async def main():
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    env = os.environ.copy()
    env['PYTHONUTF8'] = '1'
    params = StdioServerParameters(command=str(ROOT / '.venv/Scripts/python.exe'),
                                  args=[str(ROOT / 'mcp/server.py')], cwd=str(ROOT), env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = sorted(t.name for t in tools.tools)
            assert names == ['clone_voice', 'delete_voice', 'list_voices', 'speak']
            listed = await session.call_tool('list_voices', {})
            assert not listed.isError
            print('MCP stdio tools:', ', '.join(names))
    baseline = json.loads((ROOT / 'logs/step2-baseline.json').read_text())
    changed = [p for p, h in baseline['upstream_tracked_sha256'].items()
               if hashlib.sha256((ROOT / 'VieNeu-TTS' / p).read_bytes()).hexdigest() != h]
    assert not changed, changed
    assert hashlib.sha256((ROOT / 'VieNeu-TTS/.venv/pyvenv.cfg').read_bytes()).hexdigest() == baseline['upstream_env_sha256']
    status = subprocess.check_output(['git', '-C', str(ROOT / 'VieNeu-TTS'), 'status', '--porcelain'], text=True)
    assert not status.strip(), status
    print('Upstream tracked SHA256 and pyvenv.cfg unchanged; git clean')
    listeners = [s.strip() for s in subprocess.check_output(['netstat', '-ano', '-p', 'tcp'], text=True).splitlines()
                 if 'LISTENING' in s and any(':'+str(p)+' ' in s for p in (8000,8001))]
    assert len(listeners) == 2, listeners
    assert all('127.0.0.1:' in s for s in listeners), listeners
    print('Loopback listeners:', *listeners, sep='\n')
    wav = max((ROOT / 'outputs').glob('*.wav'), key=lambda p: p.stat().st_mtime)
    with wave.open(str(wav), 'rb') as audio:
        info = {'path': str(wav), 'duration_seconds': audio.getnframes()/audio.getframerate(),
                'sample_rate': audio.getframerate(), 'channels': audio.getnchannels(), 'sample_width': audio.getsampwidth()}
    print('Latest CLI WAV:', json.dumps(info))
    (ROOT / 'logs/step2-verification.json').write_text(json.dumps({'mcp_tools': names, 'wav': info,
        'upstream_unchanged': True, 'listeners': listeners}, indent=2), encoding='utf-8')

if __name__ == '__main__':
    asyncio.run(main())
