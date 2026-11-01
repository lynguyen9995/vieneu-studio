"""Consent metadata and audio only; no model is imported or loaded."""
from __future__ import annotations
import hashlib
import io
import json
import re
import subprocess
import os
import stat
import uuid
import wave
from datetime import datetime, timezone
from pathlib import Path

NAME_PATTERN = re.compile(r'[^\W_][\w .\-]{0,63}')
MAX_BYTES = 20 * 1024 * 1024


def convert_wav(data: bytes, filename: str, ffmpeg: str | None) -> bytes:
    if not data or len(data) > MAX_BYTES:
        raise ValueError('Audio must be nonempty and at most 20 MiB')
    ext = Path(filename).suffix.lower()
    formats = {'.wav': 'wav', '.mp3': 'mp3', '.flac': 'flac', '.ogg': 'ogg', '.m4a': 'mov'}
    if ext not in formats:
        raise ValueError('Use WAV, MP3, FLAC, OGG or M4A')
    if ffmpeg:
        try:
            cp = subprocess.run([ffmpeg, '-nostdin', '-hide_banner', '-loglevel', 'error',
                '-protocol_whitelist', 'pipe', '-f', formats[ext], '-i', 'pipe:0',
                '-t', '31', '-vn', '-ac', '1', '-ar', '48000', '-c:a', 'pcm_s16le',
                '-f', 'wav', 'pipe:1'], input=data, capture_output=True, timeout=30,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except (OSError, subprocess.TimeoutExpired) as e:
            raise ValueError('ffmpeg unavailable or conversion timed out; check FFMPEG_PATH') from e
        if cp.returncode:
            raise ValueError('ffmpeg could not decode this clip')
        data = cp.stdout
    elif ext != '.wav':
        raise ValueError('ffmpeg missing: supply PCM WAV or configure existing FFMPEG_PATH; nothing installed automatically')
    try:
        with wave.open(io.BytesIO(data), 'rb') as w:
            channels, width, rate = w.getnchannels(), w.getsampwidth(), w.getframerate()
            pcm = w.readframes(rate * 31)
        duration = len(pcm) / channels / width / rate
        if not 0 < duration <= 30 or width != 2 or channels != 1:
            raise ValueError('Use nonempty mono PCM16 WAV up to 30 seconds; 3–8 seconds recommended')
        out = io.BytesIO()
        with wave.open(out, 'wb') as w:
            w.setparams((channels, width, rate, 0, 'NONE', 'not compressed'))
            w.writeframes(pcm)
        return out.getvalue()
    except (wave.Error, EOFError, ZeroDivisionError) as e:
        raise ValueError('Invalid PCM WAV; configure ffmpeg for conversion') from e


class VoiceStore:
    def __init__(self, root: Path):
        self.directory = Path(root).resolve() / 'voices'
        self.reject_link(self.directory)
        self.directory.mkdir(exist_ok=True)
        self.deleted_file = self.checked('.deleted')
        self.deleted = set(json.loads(self.deleted_file.read_text(encoding='utf-8'))) if self.deleted_file.exists() else set()
        for name in self.deleted:
            if not isinstance(name, str) or not NAME_PATTERN.fullmatch(name):
                raise ValueError('Invalid deleted voice state')
            self.checked(self.slug(name) + '.json').unlink(missing_ok=True)
            self.checked(self.slug(name) + '.wav').unlink(missing_ok=True)

    @staticmethod
    def reject_link(path):
        try:
            info = path.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400 or info.st_nlink > 1:
            raise ValueError('Linked/reparse voice state refused')

    def checked(self, name):
        self.reject_link(self.directory)
        path = self.directory / name
        if path.parent != self.directory or path.name != name:
            raise ValueError('Voice state path outside store')
        self.reject_link(path)
        return path

    def atomic_json(self, name, value):
        target = self.checked(name)
        temp = self.checked('.write-' + uuid.uuid4().hex)
        try:
            with temp.open('x', encoding='utf-8') as f:
                json.dump(value, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            self.checked(name)
            temp.replace(target)
        finally:
            temp.unlink(missing_ok=True)

    def preset_catalog(self, initial):
        path = self.checked('.presets')
        if path.exists():
            return json.loads(path.read_text(encoding='utf-8'))
        self.atomic_json('.presets', initial)
        return initial

    def records(self) -> dict[str, dict]:
        out = {}
        for p in self.directory.glob('*.json'):
            self.checked(p.name)
            self.checked(p.with_suffix('.wav').name)
            meta = json.loads(p.read_text(encoding='utf-8'))
            name = meta['name']
            if not NAME_PATTERN.fullmatch(name) or p.stem != self.slug(name) or not meta.get('consent_confirmed') or not meta.get('consent_note', '').strip():
                raise ValueError('Invalid voice metadata or missing owner consent')
            if not p.with_suffix('.wav').is_file():
                raise ValueError('Stored voice audio is missing')
            out[name] = meta
        return out

    @staticmethod
    def slug(name):
        prefix = re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')[:32] or 'voice'
        return prefix + '-' + hashlib.sha256(name.encode('utf-8')).hexdigest()[:12]

    def audio_path(self, name):
        return self.checked(self.slug(name) + '.wav')

    def save(self, name, audio, description, consent_note, denoise):
        meta = {'name': name, 'created_at': datetime.now(timezone.utc).isoformat(),
                'source_description': description, 'description': description,
                'consent_confirmed': True, 'consent_note': consent_note, 'denoise': denoise}
        wav = self.audio_path(name)
        path = self.checked(wav.with_suffix('.json').name)
        if wav.exists() or path.exists():
            raise ValueError('Stored voice name already exists; delete it explicitly first')
        # Persist a pending tombstone first: interrupted publication is recovered at startup.
        self.deleted.add(name)
        self.save_deleted()
        try:
            with wav.open('xb') as f:
                f.write(audio)
                f.flush()
                os.fsync(f.fileno())
            self.atomic_json(path.name, meta)
            self.deleted.discard(name)
            self.save_deleted()
        except Exception:
            self.deleted.add(name)
            wav.unlink(missing_ok=True)
            path.unlink(missing_ok=True)
            raise
        return meta

    def save_deleted(self):
        self.atomic_json('.deleted', sorted(self.deleted))

    def delete(self, name):
        # Persist the tombstone before removing committed records. Crash-safe fail closed.
        self.deleted.add(name)
        self.save_deleted()
        self.checked(self.slug(name) + '.json').unlink(missing_ok=True)
        self.audio_path(name).unlink(missing_ok=True)

    def artifacts_exist(self, name):
        return self.audio_path(name).exists() or self.checked(self.slug(name) + '.json').exists()
