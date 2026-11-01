"""Loopback-only HTTP adapter; no model imports or extra GPU allocation."""
import asyncio
import hmac
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from starlette.middleware.cors import CORSMiddleware
from fastapi.responses import Response, JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool
from gateway.store import VoiceStore, NAME_PATTERN, MAX_BYTES, convert_wav
from gateway.translate import Translator, TranslatorUnavailable, MAX_LINES, MAX_LINE_CHARS, MAX_TOTAL_CHARS
from gateway.webapp import (WEB_HOSTS, SELF_ORIGINS, PREVIEW_NAME, is_webapp_path,
                            is_public_webapp_path, mount_webapp)


def create_app(settings, *, transport=None, web_hosts=WEB_HOSTS, engine_ctl=None, translate_transport=None):
    store = VoiceStore(settings.root)
    translator = Translator(settings.ollama_url, settings.translate_model, transport=translate_transport)
    lock = asyncio.Lock()

    async def enroll(client, name, audio, meta):
        return await client.post('/v1/voices', data={'name': name, 'denoise': str(meta.get('denoise', True)).lower(),
            'description': ''}, files={'file': ('reference.wav', audio, 'audio/wav')})

    @asynccontextmanager
    async def lifespan(app):
        async with httpx.AsyncClient(base_url=settings.upstream, transport=transport,
                                    timeout=httpx.Timeout(120, connect=5), trust_env=False) as client:
            app.state.upstream = client
            for attempt in range(60):
                try:
                    r = await client.get('/health')
                    if r.status_code == 200 and r.json().get('status') == 'ok':
                        break
                except (httpx.HTTPError, ValueError):
                    pass
                await asyncio.sleep(1)
            else:
                raise RuntimeError('Upstream unavailable: start start-all.bat')
            r = await client.get('/v1/voices')
            r.raise_for_status()
            # Preserve preset identifiers across gateway-only restarts, so residual custom RAM
            # entries cannot accidentally become protected presets. First snapshot from fresh install.
            app.state.presets = store.preset_catalog(r.json()['data'])
            failed = await replay(client)
            if failed:
                raise RuntimeError('Stored voice replay failed; check upstream and restart start-all.bat')
            try:
                yield
            finally:
                await translator.aclose()

    async def replay(client):
        """Enroll every stored clone into the engine (engines keep clones in RAM only)."""
        failed = []
        for name, meta in store.records().items():
            if name in store.deleted:
                continue
            r = await enroll(client, name, store.audio_path(name).read_bytes(), meta)
            if r.status_code != 200:
                failed.append(name)
        return failed

    async def resync_fresh_engine():
        """After a model switch (fresh engine process): re-snapshot presets, then replay clones."""
        client = app.state.upstream
        r = await client.get('/v1/voices')
        r.raise_for_status()
        presets = r.json()['data']
        store.atomic_json('.presets', presets)
        app.state.presets = presets
        return await replay(client)

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store = store
    app.state.settings = settings
    app.state.resync_fresh_engine = resync_fresh_engine

    def protected_names():
        return {n for v in app.state.presets for n in [v['id'], *(v.get('aliases') or [])]}

    @app.middleware('http')
    async def authenticate(request, call_next):
        origin = request.headers.get('origin')
        if origin and origin not in settings.origins and origin not in SELF_ORIGINS:
            return JSONResponse({'error': 'Origin not allowed'}, status_code=403)
        path = request.url.path
        if is_webapp_path(path) and request.headers.get('host', '') not in web_hosts:
            return JSONResponse({'error': 'Open the studio via http://127.0.0.1:8001/app/'}, status_code=403)
        if path not in ('/health', '/v1/pair') and not is_public_webapp_path(path):
            supplied = request.headers.get('authorization', '')
            if not settings.token or not hmac.compare_digest(supplied.encode(), ('Bearer ' + settings.token).encode()):
                return JSONResponse({'error': 'Invalid or missing bearer token'}, status_code=401)
        try:
            return await call_next(request)
        except httpx.HTTPError:
            return JSONResponse({'error': 'Upstream unavailable; start start-all.bat'}, status_code=503)

    @app.get('/health')
    async def health():
        r = await app.state.upstream.get('/health')
        return JSONResponse({**r.json(), 'service': 'vieneu-gateway', 'engine': app.state.engine_status()},
                            status_code=r.status_code)

    @app.get('/v1/models')
    async def models():
        r = await app.state.upstream.get('/v1/models')
        return Response(r.content, status_code=r.status_code, media_type='application/json')

    @app.get('/v1/voices')
    async def voices():
        records = store.records()
        data = [dict(v, kind='preset') for v in app.state.presets]
        data += [dict(id=n, name=n, description=m['source_description'], kind='cloned')
                 for n, m in records.items() if n not in store.deleted]
        return {'object': 'list', 'data': data}

    @app.post('/v1/voices')
    async def clone(name: str = Form(...), file: UploadFile = File(...), denoise: bool = Form(True),
                    description: str = Form(''), consent_confirmed: bool = Form(False), consent_note: str = Form('')):
        if not consent_confirmed or not consent_note.strip():
            raise HTTPException(403, 'Voice owner must have agreed to cloning; explicit consent and note required')
        if not NAME_PATTERN.fullmatch(name):
            raise HTTPException(400, 'Invalid voice name: 1–64 letters/digits then spaces, dots, hyphens or underscores')
        if len(description) > 2000 or len(consent_note) > 4000:
            raise HTTPException(400, 'Description or consent note too long')
        async with lock:
            if name in protected_names() or name.lower() == PREVIEW_NAME or store.artifacts_exist(name):
                raise HTTPException(409, 'Preset/alias or stored name exists; presets cannot be replaced')
            raw = await file.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise HTTPException(413, 'Reference exceeds 20 MiB')
            try:
                audio = await run_in_threadpool(convert_wav, raw, file.filename or '', settings.ffmpeg)
            except ValueError as e:
                raise HTTPException(400, str(e)) from e
            meta = {'denoise': denoise}
            r = await enroll(app.state.upstream, name, audio, meta)
            if r.status_code != 200:
                return Response(r.content, status_code=r.status_code, media_type='application/json')
            try:
                store.save(name, audio, description, consent_note, denoise)
            except (OSError, ValueError) as e:
                store.deleted.add(name)
                store.save_deleted()
                raise HTTPException(500, 'Voice could not be persisted; upstream may retain it until restart') from e
            return {'id': name, 'name': name, 'description': description, 'kind': 'cloned', 'persisted': True}

    @app.delete('/v1/voices/{name}')
    async def delete(name: str):
        if not NAME_PATTERN.fullmatch(name):
            raise HTTPException(400, 'Invalid voice name')
        async with lock:
            if name in protected_names():
                raise HTTPException(409, 'Presets and their aliases cannot be deleted')
            if name not in store.deleted and not store.artifacts_exist(name):
                raise HTTPException(404, 'Stored cloned voice not found')
            store.delete(name)
        return {'deleted': name, 'upstream_removed': False,
                'note': 'Upstream has no delete endpoint; residual RAM clears on upstream restart and is blocked here'}

    @app.post('/v1/audio/speech')
    async def speech(request: Request):
        try:
            data = await request.json()
        except ValueError as e:
            raise HTTPException(400, 'Invalid JSON') from e
        if not isinstance(data, dict):
            raise HTTPException(400, 'JSON object required')
        name = data.get('voice')
        if name is not None and not isinstance(name, str):
            raise HTTPException(400, 'Voice must be a string')
        if name and name not in protected_names() and (name not in store.records() or name in store.deleted):
            raise HTTPException(400, 'Unknown or deleted voice; list gateway voices first')
        client = app.state.upstream
        req = client.build_request('POST', '/v1/audio/speech', json=data)
        response = await client.send(req, stream=True)
        headers = {k: v for k, v in response.headers.items() if k.lower() in {
            'content-type', 'x-sample-rate', 'x-request-id', 'x-vieneu-ignored', 'retry-after', 'cache-control'}}
        if response.status_code != 200:
            try:
                body = await response.aread()
            finally:
                await response.aclose()
            return Response(body, status_code=response.status_code, headers=headers)
        async def chunks():
            try:
                async for chunk in response.aiter_raw():
                    yield chunk
            finally:
                await response.aclose()
        return StreamingResponse(chunks(), status_code=200, headers=headers)

    @app.post('/v1/pair')
    async def pair(request: Request):
        """Zero-config pairing: hand the token only to a browser extension listed in ALLOWED_ORIGINS.
        Browsers set Origin themselves, so websites and other extensions cannot ask for it.
        POST, because Chrome omits Origin on GET requests from an extension's service worker."""
        origin = request.headers.get('origin', '')
        if origin.startswith('chrome-extension://') and origin in settings.origins:
            return {'token': settings.token, 'gateway': 'vieneu-gateway'}
        raise HTTPException(403, 'Pairing is only for the extension listed in ALLOWED_ORIGINS')

    @app.get('/v1/translate/status')
    async def translate_status():
        return await translator.status()

    @app.post('/v1/translate')
    async def translate(request: Request):
        try:
            data = await request.json()
        except ValueError as e:
            raise HTTPException(400, 'Invalid JSON') from e
        texts = data.get('texts') if isinstance(data, dict) else None
        if not isinstance(texts, list) or not texts or not all(isinstance(t, str) for t in texts):
            raise HTTPException(400, 'texts must be a non-empty list of strings')
        if len(texts) > MAX_LINES or any(len(t) > MAX_LINE_CHARS for t in texts) or sum(map(len, texts)) > MAX_TOTAL_CHARS:
            raise HTTPException(413, f'Too much text: max {MAX_LINES} lines, {MAX_LINE_CHARS} chars per line')
        target = str(data.get('target') or 'vi')[:16]
        try:
            group = max(1, min(12, int(data.get('group', 6))))
        except (TypeError, ValueError):
            group = 6
        try:
            out = await translator.lines(texts, target, group)
        except TranslatorUnavailable as e:
            raise HTTPException(503, str(e)) from e
        return {'translations': out, 'model': translator.model, 'target': target}

    mount_webapp(app, settings, **({'ctl': engine_ctl} if engine_ctl else {}))

    app.add_middleware(CORSMiddleware, allow_origins=list(settings.origins),
                       allow_methods=['GET', 'POST', 'DELETE', 'OPTIONS'],
                       allow_headers=['Authorization', 'Content-Type'],
                       expose_headers=['X-Sample-Rate', 'X-Request-Id', 'X-VieNeu-Ignored'],
                       allow_credentials=False)
    return app
