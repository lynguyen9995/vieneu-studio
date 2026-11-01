"""Text translation through a local Ollama model (default `hy-mt2` = Tencent Hy-MT2-1.8B, Apache-2.0).

The gateway never loads a model itself: it calls Ollama on loopback. Lines are translated in small
groups (one request per group) so the model sees neighbouring lines as context, which keeps pronouns
and tone consistent. If the model does not return exactly one line per input line, that group falls
back to line-by-line translation, so the output always lines up with the input. The same happens
when the group answer looks shifted: a line repeated although the inputs differ (the model merged
two lines and repeated one to keep the count), or a line far longer/shorter than its source.
"""
from __future__ import annotations

import asyncio
import re
from urllib.parse import urlsplit

import httpx

LANG_NAMES = {'vi': 'Vietnamese', 'en': 'English', 'ko': 'Korean', 'zh': 'Chinese', 'ja': 'Japanese',
              'fr': 'French', 'de': 'German', 'es': 'Spanish', 'th': 'Thai', 'ru': 'Russian'}
ONE = 'Translate the following text into {target}, only output the translated result without additional explanation.\n\n{text}'
MANY = ('Translate the following text into {target}. It has {n} lines: translate it line by line and output '
        'exactly {n} lines in the same order, only the translated result without additional explanation.\n\n{text}')
MAX_LINES, MAX_LINE_CHARS, MAX_TOTAL_CHARS = 300, 2000, 40000


def _key(text: str) -> str:
    return re.sub(r'\W+', '', text.lower())


def suspicious(src: list[str], out: list[str]) -> bool:
    """True when a group translation probably does not line up with its source lines."""
    if len({_key(o) for o in out}) < len(out) and len({_key(s) for s in src}) == len(src):
        return True
    return any(len(o) > 4 * len(s) + 20 or len(o) < 0.15 * len(s) for s, o in zip(src, out))


class TranslatorUnavailable(RuntimeError):
    pass


def loopback_url(url: str) -> str:
    u = urlsplit(url)
    if u.scheme != 'http' or u.hostname not in ('127.0.0.1', 'localhost', '::1'):
        raise ValueError('OLLAMA_URL must be an http://127.0.0.1:<port> address')
    return url.rstrip('/')


def clean(text: str) -> str:
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in '"“”\'':
        text = text[1:-1].strip()
    return text


class Translator:
    def __init__(self, base_url: str, model: str, transport=None):
        self.base_url, self.model, self._transport = loopback_url(base_url), model, transport
        self._client: httpx.AsyncClient | None = None
        self._lock = asyncio.Lock()      # one request at a time: keeps VRAM use predictable on a 6 GB card

    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(base_url=self.base_url, transport=self._transport, trust_env=False,
                                             timeout=httpx.Timeout(180, connect=5))
        return self._client

    async def aclose(self):
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def status(self) -> dict:
        try:
            r = await self.client().get('/api/tags')
            names = {m.get('name', '') for m in r.json().get('models', [])}
        except (httpx.HTTPError, ValueError):
            return {'ok': False, 'model': self.model, 'error': 'Ollama không chạy (127.0.0.1:11434).'}
        if self.model not in names and f'{self.model}:latest' not in names:
            return {'ok': False, 'model': self.model, 'error': f'Chưa có model {self.model} trong Ollama.'}
        return {'ok': True, 'model': self.model}

    async def _ask(self, prompt: str) -> str:
        body = {'model': self.model, 'messages': [{'role': 'user', 'content': prompt}], 'stream': False,
                'keep_alive': '10m', 'options': {'num_predict': 2048}}
        try:
            async with self._lock:
                r = await self.client().post('/api/chat', json=body)
        except httpx.HTTPError as e:
            raise TranslatorUnavailable('Không gọi được Ollama (127.0.0.1:11434). Mở Ollama rồi thử lại.') from e
        if r.status_code == 404:
            raise TranslatorUnavailable(f'Ollama chưa có model "{self.model}". Chạy: ollama create hy-mt2 -f models\\hy-mt2.Modelfile')
        if r.status_code != 200:
            raise TranslatorUnavailable(f'Ollama lỗi {r.status_code}.')
        try:
            return r.json()['message']['content']
        except (ValueError, KeyError, TypeError) as e:
            raise TranslatorUnavailable('Ollama trả về dữ liệu lạ.') from e

    async def one(self, text: str, target: str) -> str:
        if not text.strip():
            return text
        return clean(await self._ask(ONE.format(target=target, text=text.strip())))

    async def lines(self, texts: list[str], target_code: str = 'vi', group: int = 6) -> list[str]:
        target = LANG_NAMES.get(target_code, target_code)
        out: list[str] = []
        for i in range(0, len(texts), max(1, group)):
            chunk = [re.sub(r'\s+', ' ', t).strip() for t in texts[i:i + group]]
            idx = [k for k, t in enumerate(chunk) if t]
            result = list(chunk)
            if len(idx) > 1:
                joined = '\n'.join(chunk[k] for k in idx)
                got = [clean(x) for x in (await self._ask(MANY.format(target=target, n=len(idx), text=joined))).splitlines() if x.strip()]
                if len(got) == len(idx) and not suspicious([chunk[k] for k in idx], got):
                    for k, g in zip(idx, got):
                        result[k] = g
                    out.extend(result)
                    continue
            for k in idx:                                   # single line, or the group did not line up
                result[k] = await self.one(chunk[k], target)
            out.extend(result)
        return out
