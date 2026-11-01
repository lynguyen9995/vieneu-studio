"""Speech-to-text core for VieNeu Studio. No web framework in here, so it is easy to test with fakes.

Recognizer  faster-whisper (default large-v3-turbo), loaded on first use and unloaded after `idle` seconds
            without work, so the VRAM goes back to the voice engine and the translator (an RTX 3060 6 GB is
            shared by all three). Falls back to CPU int8 when CUDA cannot be used.
Preparer    "listen ahead" for a YouTube video: yt-dlp downloads the AUDIO ONLY into a temp folder, Whisper
            transcribes it and every segment is published as soon as it is recognised (the extension polls),
            the audio file is deleted at the end. Only the text is kept, in a small capped cache.
transcribe_bytes  one chunk of audio (any container ffmpeg/PyAV can read) → segments; used by live listening.
"""
from __future__ import annotations

import gc
import io
import json
import os
import queue
import re
import shutil
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

VIDEO_ID = re.compile(r'[A-Za-z0-9_-]{11}')
MAX_SECONDS = 3 * 3600                 # longer videos: not downloaded (live listening instead)
MIN_FREE_BYTES = 2 * 1024 ** 3         # keep at least 2 GB free on the disk holding the temp folder
CACHE_LIMIT = 50 * 1024 ** 2           # transcripts kept for re-watching / seeking; oldest removed first
MAX_CHUNK_BYTES = 25 * 1024 ** 2       # one live chunk (also the OpenAI transcription upload limit)
MAX_JOBS_IN_MEMORY = 20


class SttError(RuntimeError):
    """An expected failure with a short machine code and a message for the user (Vietnamese)."""
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def add_cuda_libraries() -> None:
    """Make the cuBLAS shipped by the `nvidia-cublas-cu12` wheel findable by CTranslate2.
    Windows: add its bin/ folders to the DLL search path. Linux: preload the libraries by absolute path."""
    try:
        import nvidia  # namespace package of the nvidia-* wheels
    except ImportError:
        return
    for base in [Path(p) for p in getattr(nvidia, '__path__', [])]:
        if os.name == 'nt':
            for d in base.glob('*/bin'):
                os.add_dll_directory(str(d))
                os.environ['PATH'] = str(d) + os.pathsep + os.environ.get('PATH', '')
        else:
            import ctypes
            for name in ('libcublasLt.so.12', 'libcublas.so.12'):
                for lib in base.glob(f'*/lib/{name}'):
                    try:
                        ctypes.CDLL(str(lib), mode=ctypes.RTLD_GLOBAL)
                    except OSError:
                        pass


def load_faster_whisper(model_name: str, device: str, download_root: str | None):
    """Real loader: returns (model, device_used). Tries CUDA first (int8_float16), then CPU (int8)."""
    add_cuda_libraries()
    try:
        from faster_whisper import WhisperModel
    except ImportError as e:
        raise SttError('not_installed', 'Chưa cài faster-whisper. Trong thư mục vieneu-studio chạy: uv sync') from e
    tries = [('cuda', 'int8_float16'), ('cpu', 'int8')] if device == 'auto' else [
        (device, 'int8_float16' if device == 'cuda' else 'int8')]
    errors = []
    for dev, compute in tries:
        try:
            return WhisperModel(model_name, device=dev, compute_type=compute, download_root=download_root), dev
        except Exception as e:  # CUDA missing/out of memory → next option
            errors.append(f'{dev}: {type(e).__name__}: {e}'[:300])
    raise SttError('model_failed', 'Không nạp được Whisper. ' + ' | '.join(errors))


class Recognizer:
    def __init__(self, model_name='large-v3-turbo', device='auto', idle=600, loader=None, download_root=None,
                 clock=time.monotonic):
        self.model_name, self.device, self.idle, self.download_root = model_name, device, idle, download_root
        self.loader = loader or (lambda: load_faster_whisper(model_name, device, download_root))
        self.clock = clock
        self._model = None
        self.device_used = None
        self.last_error = None
        self._users = 0
        self._last_used = clock()
        self._lock = threading.Lock()

    @contextmanager
    def use(self):
        """Hold the model for one job; loads it if needed. Never unloaded while someone uses it."""
        with self._lock:
            if self._model is None:
                try:
                    self._model, self.device_used = self.loader()
                    self.last_error = None
                except SttError as e:
                    self.last_error = str(e)
                    raise
            self._users += 1
            model = self._model
        try:
            yield model
        finally:
            with self._lock:
                self._users -= 1
                self._last_used = self.clock()

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def unload_if_idle(self) -> bool:
        with self._lock:
            if self._model is None or self._users or self.clock() - self._last_used < self.idle:
                return False
            self._model = None
        gc.collect()                                   # CTranslate2 frees its VRAM when the model object goes
        return True

    def start_reaper(self, every=30):
        def loop():
            while True:
                time.sleep(every)
                self.unload_if_idle()
        threading.Thread(target=loop, name='stt-idle-unload', daemon=True).start()

    def status(self) -> dict:
        return {'model': self.model_name, 'loaded': self.loaded, 'device': self.device_used,
                'idle_unload_seconds': self.idle, 'error': self.last_error}


def _segments(raw):
    for s in raw:
        text = (s.text or '').strip()
        if text:
            yield {'start': round(float(s.start), 2), 'end': round(float(s.end), 2), 'text': text}


def transcribe_bytes(recognizer: Recognizer, data: bytes, language: str | None = None, decode=None) -> dict:
    """One chunk of audio → {language, duration, segments, text}. Greedy decoding: live chunks must be quick."""
    if not data:
        raise SttError('empty', 'Không có âm thanh.')
    if len(data) > MAX_CHUNK_BYTES:
        raise SttError('too_big', 'Đoạn âm thanh quá lớn (tối đa 25 MB).')
    if decode is None:
        try:
            from faster_whisper.audio import decode_audio
        except ImportError as e:
            raise SttError('not_installed', 'Chưa cài faster-whisper. Trong thư mục vieneu-studio chạy: uv sync') from e
        decode = lambda b: decode_audio(io.BytesIO(b), sampling_rate=16000)
    try:
        audio = decode(data)
    except Exception as e:
        raise SttError('bad_audio', f'Không đọc được âm thanh: {type(e).__name__}') from e
    with recognizer.use() as model:
        raw, info = model.transcribe(audio, language=language or None, beam_size=1, vad_filter=True,
                                     condition_on_previous_text=False)
        segs = list(_segments(raw))
    return {'language': info.language, 'duration': round(float(info.duration or 0), 2), 'segments': segs,
            'text': ' '.join(s['text'] for s in segs)}


def _deno_on_path() -> None:
    """yt-dlp needs a JavaScript runtime (Deno) for YouTube. A fresh winget install is not on the PATH of
    processes started before it, so look in the usual places."""
    if shutil.which('deno'):
        return
    home, local = Path.home(), Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData' / 'Local'))
    candidates = [home / '.deno' / 'bin', local / 'Microsoft' / 'WinGet' / 'Links']
    candidates += sorted((local / 'Microsoft' / 'WinGet' / 'Packages').glob('DenoLand.Deno*'))
    for d in candidates:
        if (d / 'deno.exe').exists() or (d / 'deno').exists():
            os.environ['PATH'] = str(d) + os.pathsep + os.environ.get('PATH', '')
            return


def _short(e: Exception) -> str:
    msg = re.sub(r'\x1b\[[0-9;]*m', '', str(e)).replace('ERROR: ', '').strip()
    return msg.splitlines()[0][:240] if msg else type(e).__name__


def ytdlp_download(video_id: str, folder: Path, max_seconds: int) -> tuple[Path, dict]:
    """Audio only, smallest good-for-speech format. The URL is built here from a validated id (no other URLs)."""
    try:
        import yt_dlp
    except ImportError as e:
        raise SttError('not_installed', 'Chưa cài yt-dlp. Trong thư mục vieneu-studio chạy: uv sync') from e
    _deno_on_path()
    url = f'https://www.youtube.com/watch?v={video_id}'
    opts = {'quiet': True, 'no_warnings': True, 'noprogress': True, 'noplaylist': True, 'cachedir': False,
            'format': 'bestaudio[acodec=opus][abr<=96]/bestaudio[abr<=128]/bestaudio/best',
            'outtmpl': str(folder / f'{video_id}.%(ext)s'), 'max_filesize': 500 * 1024 ** 2}
    with yt_dlp.YoutubeDL(opts) as ydl:
        try:
            info = ydl.extract_info(url, download=False)
        except Exception as e:
            hint = ' Cài Deno: winget install --id DenoLand.Deno' if 'javascript' in str(e).lower() or 'challenge' in str(e).lower() else ''
            raise SttError('download_failed', f'yt-dlp không lấy được video: {_short(e)}.{hint}') from e
        if info.get('is_live'):
            raise SttError('live', 'Video đang phát trực tiếp: không tải trước được.')
        duration = info.get('duration') or 0
        if duration > max_seconds:
            raise SttError('too_long', f'Video dài {duration // 60} phút, quá giới hạn {max_seconds // 60} phút.')
        try:
            info = ydl.process_ie_result(info, download=True)
        except Exception as e:
            raise SttError('download_failed', f'yt-dlp tải lỗi: {_short(e)}') from e
    files = [Path(d['filepath']) for d in info.get('requested_downloads') or [] if d.get('filepath')]
    path = files[0] if files else next(iter(sorted(folder.glob(f'{video_id}.*'))), None)
    if not path or not path.exists():
        raise SttError('download_failed', 'yt-dlp không tạo ra file âm thanh.')
    return path, info


@dataclass
class Job:
    video_id: str
    state: str = 'queued'          # queued → downloading → transcribing → done | error
    segments: list = field(default_factory=list)
    language: str | None = None
    duration: float = 0.0
    progress: float = 0.0          # seconds of audio already transcribed
    code: str | None = None
    error: str | None = None
    cached: bool = False
    updated: float = field(default_factory=time.time)


class Preparer:
    def __init__(self, recognizer: Recognizer, cache_dir: Path, tmp_dir: Path, downloader=ytdlp_download,
                 max_seconds=MAX_SECONDS, min_free=MIN_FREE_BYTES, cache_limit=CACHE_LIMIT,
                 disk_usage=shutil.disk_usage, beam_size=5, autostart=True):
        self.recognizer, self.downloader = recognizer, downloader
        self.cache_dir, self.tmp_dir = Path(cache_dir), Path(tmp_dir)
        self.max_seconds, self.min_free, self.cache_limit = max_seconds, min_free, cache_limit
        self.disk_usage, self.beam_size, self.autostart = disk_usage, beam_size, autostart
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        for leftover in self.tmp_dir.iterdir():        # audio from a run that was killed mid-way
            if leftover.is_file():
                leftover.unlink(missing_ok=True)
        self.jobs: dict[str, Job] = {}
        self.lock = threading.Lock()
        self.queue: queue.Queue[str] = queue.Queue()
        self._worker = None

    # ---- public ----
    def prepare(self, video_id: str) -> dict:
        self._check(video_id)
        with self.lock:
            job = self.jobs.get(video_id)
            if job and job.state != 'error':
                return self._view(job)
            job = self._read_cache(video_id) or Job(video_id)
            self.jobs[video_id] = job                  # registered before the worker can pick it up
            self._trim()
            if job.state == 'queued':
                self.queue.put(video_id)
                if self.autostart:
                    self._ensure_worker()
            return self._view(job)

    def get(self, video_id: str, since: int = 0) -> dict | None:
        self._check(video_id)
        with self.lock:
            job = self.jobs.get(video_id)
            if job is None:
                job = self._read_cache(video_id)
                if job is None:
                    return None
                self.jobs[video_id] = job
            return self._view(job, since)

    def run_pending(self) -> None:
        """Process queued jobs in this thread (tests)."""
        while not self.queue.empty():
            self._run(self.queue.get())

    # ---- internals ----
    @staticmethod
    def _check(video_id):
        if not isinstance(video_id, str) or not VIDEO_ID.fullmatch(video_id):
            raise SttError('bad_id', 'Mã video YouTube không hợp lệ.')

    def _view(self, job: Job, since: int = 0) -> dict:
        since = max(0, int(since or 0))
        return {'video_id': job.video_id, 'state': job.state, 'language': job.language,
                'duration': round(job.duration, 2), 'progress': round(job.progress, 2), 'cached': job.cached,
                'segments': job.segments[since:], 'next': len(job.segments), 'code': job.code, 'error': job.error}

    def _ensure_worker(self):
        if self._worker is None or not self._worker.is_alive():
            self._worker = threading.Thread(target=self._loop, name='stt-prepare', daemon=True)
            self._worker.start()

    def _loop(self):
        while True:
            self._run(self.queue.get())

    def _set(self, job, **kw):
        with self.lock:
            for k, v in kw.items():
                setattr(job, k, v)
            job.updated = time.time()

    def _run(self, video_id: str):
        with self.lock:
            job = self.jobs.get(video_id)
        if job is None or job.state != 'queued':
            return
        path = None
        try:
            if self.disk_usage(self.tmp_dir).free < self.min_free:
                raise SttError('low_disk', 'Ổ đĩa còn dưới 2 GB trống: không tải âm thanh.')
            self._set(job, state='downloading')
            path, info = self.downloader(video_id, self.tmp_dir, self.max_seconds)
            self._set(job, state='transcribing', duration=float(info.get('duration') or 0))
            with self.recognizer.use() as model:
                raw, meta = model.transcribe(str(path), beam_size=self.beam_size, vad_filter=True,
                                             condition_on_previous_text=False)
                self._set(job, language=meta.language, duration=float(meta.duration or job.duration))
                for seg in _segments(raw):
                    with self.lock:
                        job.segments.append(seg)
                        job.progress = seg['end']
                        job.updated = time.time()
            self._set(job, state='done', progress=job.duration)
            self._write_cache(job)
        except SttError as e:
            self._set(job, state='error', code=e.code, error=str(e))
        except Exception as e:                       # never let one video kill the worker
            self._set(job, state='error', code='failed', error=f'{type(e).__name__}: {_short(e)}')
        finally:
            for p in [path, *self.tmp_dir.glob(f'{video_id}.*')]:
                if p:
                    try:
                        Path(p).unlink(missing_ok=True)
                    except OSError:
                        pass

    def _cache_file(self, video_id) -> Path:
        return self.cache_dir / f'{video_id}.json'

    def _read_cache(self, video_id) -> Job | None:
        try:
            data = json.loads(self._cache_file(video_id).read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return None
        if data.get('model') != self.recognizer.model_name:
            return None
        return Job(video_id, state='done', segments=data.get('segments') or [], language=data.get('language'),
                   duration=float(data.get('duration') or 0), progress=float(data.get('duration') or 0), cached=True)

    def _write_cache(self, job: Job):
        tmp = self._cache_file(job.video_id).with_suffix('.tmp')
        tmp.write_text(json.dumps({'video_id': job.video_id, 'model': self.recognizer.model_name,
                                   'language': job.language, 'duration': job.duration, 'segments': job.segments,
                                   'created': int(time.time())}, ensure_ascii=False), encoding='utf-8')
        tmp.replace(self._cache_file(job.video_id))
        files = sorted(self.cache_dir.glob('*.json'), key=lambda p: p.stat().st_mtime)
        total = sum(p.stat().st_size for p in files)
        while files and total > self.cache_limit:
            old = files.pop(0)
            total -= old.stat().st_size
            old.unlink(missing_ok=True)

    def _trim(self):
        if len(self.jobs) <= MAX_JOBS_IN_MEMORY:
            return
        finished = sorted((j for j in self.jobs.values() if j.state in ('done', 'error')), key=lambda j: j.updated)
        for j in finished[:len(self.jobs) - MAX_JOBS_IN_MEMORY]:
            del self.jobs[j.video_id]
