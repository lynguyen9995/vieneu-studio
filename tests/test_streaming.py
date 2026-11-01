import threading
import time
import httpx
from fastapi.testclient import TestClient
import pytest
from gateway.app import create_app
from gateway.config import Settings


@pytest.mark.parametrize('format,stream_format,media,parts', [
    ('pcm', 'audio', 'audio/pcm', [b'\x01\x00' * 100, b'\x02\x00' * 100]),
    ('wav', 'audio', 'audio/wav', [b'RIFF header', b'PCM content']),
    ('pcm', 'sse', 'text/event-stream', [b'data: {"type":"speech.audio.delta","audio":"AAA="}\n\n', b'data: {"type":"speech.audio.done"}\n\n']),
])
def test_response_stream_reaches_client_before_upstream_finishes(tmp_path, format, stream_format, media, parts):
    release = threading.Event()
    closed = threading.Event()
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield parts[0]
            import asyncio
            while not release.is_set():
                await asyncio.sleep(.005)
            yield parts[1]
        async def aclose(self):
            closed.set()
    def upstream(req):
        if req.url.path == '/v1/audio/speech':
            return httpx.Response(200, headers={'Content-Type': media, 'X-Sample-Rate': '48000'}, stream=Stream())
        if req.url.path == '/health':
            return httpx.Response(200, json={'status': 'ok'})
        return httpx.Response(200, json={'data': [{'id': 'Mai Anh', 'name': 'Mai Anh'}]})
    app = create_app(Settings(root=tmp_path, token='test-only'), transport=httpx.MockTransport(upstream))
    # TestClient buffers HTTP bodies; exercise its application ASGI callable directly
    # to assert bytes are sent before the producer's second chunk is available.
    import asyncio
    async def exercise():
        async with app.router.lifespan_context(app):
            messages = []
            received = False
            async def receive():
                nonlocal received
                if not received:
                    received = True
                    import json
                    return {'type': 'http.request', 'body': json.dumps({'input': 'test', 'voice': 'Mai Anh', 'response_format': format, 'stream_format': stream_format}).encode(), 'more_body': False}
                await asyncio.sleep(10)
                return {'type': 'http.disconnect'}
            async def send(message):
                messages.append(message)
                if message['type'] == 'http.response.body' and message.get('body'):
                    assert message['body'] == parts[0] or release.is_set()
                    release.set()
            scope = {'type': 'http', 'asgi': {'version': '3.0', 'spec_version': '2.4'}, 'http_version': '1.1', 'method': 'POST', 'scheme': 'http', 'path': '/v1/audio/speech', 'raw_path': b'/v1/audio/speech', 'query_string': b'', 'headers': [(b'authorization', b'Bearer test-only'), (b'content-type', b'application/json')], 'server': ('127.0.0.1', 8001), 'client': ('127.0.0.1', 1), 'root_path': ''}
            await asyncio.wait_for(app(scope, receive, send), timeout=3)
            assert messages[0]['status'] == 200
            assert b''.join(m.get('body', b'') for m in messages) == b''.join(parts)
            assert closed.is_set()
    asyncio.run(exercise())
