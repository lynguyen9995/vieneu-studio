"""Speech-to-text service on 127.0.0.1:8002 (loopback only, one worker).

No token here, like the engine on 8000: only the gateway on 8001 (which checks the token and CORS) calls it.
  GET  /health                 {"status":"ok","service":"vieneu-stt", model, loaded, device, ...}
  POST /prepare  {"video_id"}  start (or reuse) listening ahead for a YouTube video
  GET  /jobs/{video_id}?since=N  state + segments from index N
  POST /transcribe?language=xx  raw audio bytes (wav/webm/ogg/mp3...) → segments   (live listening)
Run:  uv run --no-sync python -m stt.server            (started by start-all.bat)
      uv run --no-sync python -m stt.server --download (fetch the model once, ~1.6 GB, then exit)
Settings (.env, optional): STT_MODEL=large-v3-turbo  STT_DEVICE=auto|cuda|cpu  STT_IDLE_SECONDS=600
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from stt.core import MAX_CHUNK_BYTES, Preparer, Recognizer, SttError, transcribe_bytes

PORT = 8002
STATUS = {'bad_id': 400, 'empty': 400, 'bad_audio': 400, 'too_big': 413, 'not_installed': 503, 'model_failed': 503}


def create_app(recognizer: Recognizer, preparer: Preparer, decode=None) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.exception_handler(SttError)
    async def stt_error(request, e: SttError):
        return JSONResponse({'error': str(e), 'code': e.code}, status_code=STATUS.get(e.code, 500))

    @app.get('/health')
    def health():
        return {'status': 'ok', 'service': 'vieneu-stt', 'backend': 'ctranslate2', **recognizer.status(),
                'jobs': len(preparer.jobs)}

    @app.post('/prepare')
    def prepare(body: dict):
        return preparer.prepare(body.get('video_id') if isinstance(body, dict) else None)

    @app.get('/jobs/{video_id}')
    def job(video_id: str, since: int = 0):
        view = preparer.get(video_id, since)
        if view is None:
            raise HTTPException(404, 'Chưa có yêu cầu nghe trước cho video này.')
        return view

    @app.post('/transcribe')
    async def transcribe(request: Request, language: str | None = None):
        data = await request.body()
        if len(data) > MAX_CHUNK_BYTES:
            raise SttError('too_big', 'Đoạn âm thanh quá lớn (tối đa 25 MB).')
        lang = (language or '').strip().lower()[:8] or None
        from starlette.concurrency import run_in_threadpool
        return await run_in_threadpool(transcribe_bytes, recognizer, data, lang, decode)

    return app


def build(root: Path):
    from dotenv import dotenv_values
    env = dotenv_values(root / '.env') if (root / '.env').exists() else {}
    os.environ.setdefault('HF_HOME', str(root / 'cache' / 'huggingface'))
    try:
        idle = max(60, int(env.get('STT_IDLE_SECONDS') or 600))
    except ValueError:
        idle = 600
    recognizer = Recognizer(model_name=env.get('STT_MODEL') or 'large-v3-turbo',
                            device=(env.get('STT_DEVICE') or 'auto').lower(), idle=idle)
    preparer = Preparer(recognizer, root / 'cache' / 'stt', root / 'cache' / 'stt-tmp')
    return recognizer, preparer


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    root = Path(__file__).resolve().parents[1]
    recognizer, preparer = build(root)
    if '--download' in argv:
        print(f'Loading {recognizer.model_name} (downloads it the first time, ~1.6 GB)...', flush=True)
        try:
            with recognizer.use():
                pass
        except SttError as e:
            print(e, file=sys.stderr)
            return 1
        print(f'OK: {recognizer.model_name} on {recognizer.device_used}.', flush=True)
        return 0
    import uvicorn
    recognizer.start_reaper()
    uvicorn.run(create_app(recognizer, preparer), host='127.0.0.1', port=PORT, workers=1, log_level='info')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
