"""Same-origin browser studio served by the gateway.

Public: only the static page (/app/, /app/app.js, /app/style.css), and only for loopback
Host headers (DNS-rebinding guard). The page carries the bearer token in a meta tag; a
foreign site cannot read it (no CORS grant, Host check, frame-ancestors 'none').
Everything under /app/api/ still needs the bearer token like the rest of the gateway.
"""
from __future__ import annotations

import asyncio
import html
import io
import re
import secrets
import wave
from datetime import datetime
from pathlib import Path

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response
from starlette.concurrency import run_in_threadpool

from gateway import engine as engine_module
from gateway.store import MAX_BYTES, convert_wav

WEB_HOSTS = frozenset({'127.0.0.1:8001', 'localhost:8001'})
SELF_ORIGINS = frozenset({'http://127.0.0.1:8001', 'http://localhost:8001'})
STATIC_FILES = {'app.js': 'text/javascript; charset=utf-8', 'style.css': 'text/css; charset=utf-8'}
OUTPUT_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,150}\.wav')
LABEL = re.compile(r'[^a-z0-9-]+')
PREVIEW_NAME = 'studio-preview'   # reserved upstream slot for try-before-save cloning
SAMPLE_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,80}\.wav')
MAX_OUTPUT = 300 * 1024 * 1024
CSP = ("default-src 'self'; script-src 'self' https://cdnjs.cloudflare.com https://cdn.jsdelivr.net; "
       "worker-src 'self' blob: https://cdnjs.cloudflare.com; style-src 'self' 'unsafe-inline'; "
       "img-src 'self' data:; media-src 'self' blob:; connect-src 'self' https://cdnjs.cloudflare.com; "
       "frame-ancestors 'none'; base-uri 'none'; form-action 'none'")


def is_webapp_path(path: str) -> bool:
    return path == '/app' or path.startswith('/app/')


def is_public_webapp_path(path: str) -> bool:
    return path in ('/app', '/app/') or (path.startswith('/app/') and path[5:] in STATIC_FILES)


def wav_info(data: bytes) -> tuple[float, int]:
    with wave.open(io.BytesIO(data), 'rb') as w:
        rate, frames, width, ch = w.getframerate(), w.getnframes(), w.getsampwidth(), w.getnchannels()
    if width != 2 or ch not in (1, 2) or rate < 8000:
        raise ValueError('PCM16 WAV required')
    return frames / rate, rate


def mount_webapp(app: FastAPI, settings, ctl=engine_module) -> None:
    static_dir = Path(settings.root) / 'webapp'
    outputs = Path(settings.root) / 'outputs'

    def static_headers():
        return {'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
                'Referrer-Policy': 'no-referrer', 'Content-Security-Policy': CSP, 'X-Frame-Options': 'DENY'}

    @app.get('/app')
    async def app_redirect():
        return Response(status_code=307, headers={'Location': '/app/'})

    @app.get('/app/')
    async def index():
        page = (static_dir / 'index.html').read_text(encoding='utf-8')
        page = page.replace('__VIENEU_TOKEN__', html.escape(settings.token, quote=True))
        return HTMLResponse(page, headers=static_headers())

    @app.get('/app/{name}')
    async def static(name: str):
        if name not in STATIC_FILES:
            raise HTTPException(404, 'Not found')
        return FileResponse(static_dir / name, media_type=STATIC_FILES[name], headers=static_headers())

    def output_dir() -> Path:
        outputs.mkdir(exist_ok=True)
        return outputs.resolve()

    def output_path(name: str) -> Path:
        if not OUTPUT_NAME.fullmatch(name):
            raise HTTPException(400, 'Invalid output name')
        base = output_dir()
        path = (base / name).resolve()
        if path.parent != base or not path.is_file():
            raise HTTPException(404, 'Output not found')
        return path

    @app.get('/app/api/outputs')
    async def list_outputs(limit: int = 200):
        base = output_dir()
        items = []
        for p in sorted(base.glob('*.wav'), key=lambda p: p.stat().st_mtime, reverse=True)[:max(1, min(limit, 1000))]:
            if not OUTPUT_NAME.fullmatch(p.name) or p.is_symlink():
                continue
            st = p.stat()
            duration = rate = None
            try:
                with wave.open(str(p), 'rb') as w:
                    rate = w.getframerate()
                    duration = round(w.getnframes() / rate, 2) if w.getnframes() < 2**31 - 1 else None
            except (wave.Error, EOFError, OSError):
                pass
            items.append({'name': p.name, 'size': st.st_size,
                          'modified': datetime.fromtimestamp(st.st_mtime).isoformat(timespec='seconds'),
                          'duration_seconds': duration, 'sample_rate': rate,
                          'source': 'web' if '-web-' in p.name else 'cli/mcp'})
        return {'object': 'list', 'data': items}

    @app.get('/app/api/outputs/{name}')
    async def get_output(name: str):
        return FileResponse(output_path(name), media_type='audio/wav',
                            headers={'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})

    @app.post('/app/api/outputs')
    async def save_output(request: Request, label: str = 'audio'):
        declared = int(request.headers.get('content-length') or 0)
        if declared > MAX_OUTPUT:
            raise HTTPException(413, 'Audio too large')
        body = bytearray()
        async for chunk in request.stream():
            body += chunk
            if len(body) > MAX_OUTPUT:
                raise HTTPException(413, 'Audio too large')
        try:
            duration, rate = wav_info(bytes(body))
        except (wave.Error, EOFError, ValueError) as e:
            raise HTTPException(400, 'Body must be a PCM16 WAV file') from e
        slug = LABEL.sub('-', label.lower()).strip('-')[:24] or 'audio'
        name = f"{datetime.now():%Y%m%d-%H%M%S}-web-{slug}-{secrets.token_hex(3)}.wav"
        path = output_dir() / name
        with path.open('xb') as f:
            f.write(body)
        return {'name': name, 'path': str(path), 'duration_seconds': round(duration, 2), 'sample_rate': rate}

    # ---------- model (engine) selection ----------
    root = Path(settings.root)
    eng_state = {'state': 'ready', 'message': '', 'warnings': []}

    def engine_status():
        cfg = ctl.load_config(root)
        return {'preset': cfg['preset'], 'label': ctl.OPTIONS[cfg['preset']]['label'], 'lora_path': cfg['lora_path'],
                **eng_state}

    app.state.engine_status = engine_status

    @app.get('/app/api/engine')
    async def get_engine():
        options = [{'id': k, 'label': v['label'], 'note': v['note'], 'device': v['device'],
                    'needs_path': bool(v.get('needs_path'))} for k, v in ctl.OPTIONS.items()]
        return {'current': engine_status(), 'options': options,
                'log': ctl.log_tail(root) if eng_state['state'] != 'ready' else ''}

    async def upstream_health():
        try:
            r = await app.state.upstream.get('/health')
            body = r.json()
            return body if r.status_code == 200 and body.get('status') == 'ok' else None
        except (httpx.HTTPError, ValueError):
            return None

    async def do_switch(cfg):
        try:
            pid = await run_in_threadpool(ctl.listener_pid)
            if pid:
                if await upstream_health() is None:
                    raise RuntimeError('Cổng 8000 đang bị chương trình khác giữ hoặc model bị treo. Chạy stop-all.bat rồi start-all.bat.')
                await run_in_threadpool(ctl.kill_pid, pid)
                for _ in range(60):
                    if not await run_in_threadpool(ctl.listener_pid):
                        break
                    await asyncio.sleep(0.5)
            eng_state['message'] = 'Đang tải model mới… (lần đầu có thể phải tải về vài phút)'
            proc = await run_in_threadpool(ctl.spawn, root, cfg)
            for _ in range(1800):
                if proc.poll() is not None:
                    raise RuntimeError('Model dừng khi đang tải. Xem logs/engine.log.')
                if await upstream_health() is not None:
                    break
                await asyncio.sleep(1)
            else:
                raise RuntimeError('Quá thời gian chờ model khởi động.')
            eng_state['message'] = 'Đang nạp lại các giọng đã clone…'
            failed = await app.state.resync_fresh_engine()
            eng_state.update(state='ready', message='', warnings=[f'Chưa nạp được giọng: {n}' for n in failed])
        except Exception as e:  # surfaced to the UI; details in logs/engine.log
            eng_state.update(state='error', message=str(e) or e.__class__.__name__)

    @app.post('/app/api/engine')
    async def switch_engine(request: Request):
        try:
            cfg = ctl.validate(await request.json())
        except (ValueError, AttributeError) as e:
            raise HTTPException(400, str(e) or 'Invalid model option') from e
        if eng_state['state'] == 'loading':
            raise HTTPException(409, 'Đang tải một model khác, đợi xong đã.')
        ctl.save_config(root, cfg)
        eng_state.update(state='loading', message='Đang tắt model cũ…', warnings=[])
        app.state.engine_task = asyncio.create_task(do_switch(cfg))
        return engine_status()

    # ---------- try-before-save cloning ----------
    preview_lock = asyncio.Lock()

    @app.post('/app/api/clone-preview')
    async def clone_preview(file: UploadFile = File(...), text: str = Form(...), denoise: bool = Form(True)):
        text = text.strip()
        if not text or len(text) > 400:
            raise HTTPException(400, 'Câu thử phải có từ 1 đến 400 ký tự')
        raw = await file.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise HTTPException(413, 'Reference exceeds 20 MiB')
        try:
            audio = await run_in_threadpool(convert_wav, raw, file.filename or 'clip.wav', settings.ffmpeg)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        client = app.state.upstream
        async with preview_lock:
            r = await client.post('/v1/voices', data={'name': PREVIEW_NAME, 'denoise': str(denoise).lower(), 'description': ''},
                                  files={'file': ('reference.wav', audio, 'audio/wav')})
            if r.status_code != 200:
                return Response(r.content, status_code=r.status_code, media_type='application/json')
            r = await client.post('/v1/audio/speech', json={'model': 'vieneu-v3-turbo', 'input': text,
                                  'voice': PREVIEW_NAME, 'response_format': 'pcm'})
        if r.status_code != 200:
            return Response(r.content, status_code=r.status_code, media_type='application/json')
        return Response(r.content, media_type='application/octet-stream',
                        headers={'X-Sample-Rate': r.headers.get('x-sample-rate', '48000'), 'Cache-Control': 'no-store'})

    # ---------- engine's sample reference clips (try only) ----------
    samples_dir = root / ctl.ENGINE_DIR / 'examples' / 'audio_ref'

    @app.get('/app/api/samples')
    async def samples():
        items = []
        for p in sorted(samples_dir.glob('*.wav')) if samples_dir.is_dir() else []:
            if not SAMPLE_NAME.fullmatch(p.name):
                continue
            txt = p.with_suffix('.txt')
            items.append({'name': p.name, 'text': txt.read_text(encoding='utf-8', errors='replace').strip()[:300] if txt.is_file() else ''})
        return {'data': items}

    @app.get('/app/api/samples/{name}')
    async def sample(name: str):
        if not SAMPLE_NAME.fullmatch(name) or not (samples_dir / name).is_file():
            raise HTTPException(404, 'Sample not found')
        return FileResponse(samples_dir / name, media_type='audio/wav')
