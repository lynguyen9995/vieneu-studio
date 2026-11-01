"""Project-local configuration. Never log the token or dump dotenv values."""
from dataclasses import dataclass, field
from pathlib import Path
import secrets
import shutil
from urllib.parse import urlsplit
from dotenv import dotenv_values


@dataclass
class Settings:
    root: Path
    token: str = field(repr=False)
    origins: tuple[str, ...] = ()
    upstream: str = 'http://127.0.0.1:8000'
    ffmpeg: str | None = None
    ollama_url: str = 'http://127.0.0.1:11434'
    translate_model: str = 'hy-mt2'
    stt_url: str = 'http://127.0.0.1:8002'


def load_settings(root):
    root = Path(root).resolve()
    env = root / '.env'
    if not env.exists():
        try:
            with env.open('x', encoding='utf-8') as f:
                f.write('GATEWAY_TOKEN=' + secrets.token_urlsafe(48) + '\n'
                        'GATEWAY_URL=http://127.0.0.1:8001\nALLOWED_ORIGINS=\n')
        except FileExistsError:
            pass
    values = dotenv_values(env, interpolate=False)
    token = values.get('GATEWAY_TOKEN') or ''
    if not token.strip():
        raise ValueError('GATEWAY_TOKEN is empty in .env; refusing unauthenticated startup')
    origins = tuple(x.strip() for x in (values.get('ALLOWED_ORIGINS') or '').split(',') if x.strip())
    for origin in origins:
        u = urlsplit(origin)
        if '*' in origin or u.scheme not in ('http', 'https', 'chrome-extension') or not u.netloc or u.path or u.query or u.fragment or u.username:
            raise ValueError('ALLOWED_ORIGINS must contain explicit origins only, without paths or wildcards')
    return Settings(root=root, token=token, origins=origins,
                    ffmpeg=values.get('FFMPEG_PATH') or shutil.which('ffmpeg'),
                    ollama_url=values.get('OLLAMA_URL') or 'http://127.0.0.1:11434',
                    translate_model=values.get('TRANSLATE_MODEL') or 'hy-mt2',
                    stt_url=loopback(values.get('STT_URL') or 'http://127.0.0.1:8002', 'STT_URL'))


def loopback(url, name):
    u = urlsplit(url)
    if u.scheme != 'http' or u.hostname not in ('127.0.0.1', 'localhost', '::1'):
        raise ValueError(f'{name} must be an http://127.0.0.1:<port> address')
    return url.rstrip('/')
