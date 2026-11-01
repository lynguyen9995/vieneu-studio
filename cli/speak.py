"""HTTP-only VieNeu gateway client; no model or upstream imports."""
from pathlib import Path
import os
import struct
import wave
from datetime import datetime, timezone
from uuid import uuid4

import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]


class ClientError(RuntimeError):
    """A readable, safe-to-display client failure."""


def readable_errors(method):
    from functools import wraps
    @wraps(method)
    def wrapped(*args, **kwargs):
        try:
            return method(*args, **kwargs)
        except httpx.HTTPStatusError as error:
            status = error.response.status_code
            hint = ' Check GATEWAY_TOKEN in the project .env.' if status in (401, 403) else ''
            raise ClientError(f'Gateway HTTP {status}.{hint}') from None
        except httpx.RequestError:
            raise ClientError('Gateway unavailable or connection interrupted; start start-all.bat and retry.') from None
        except (OSError, wave.Error, ValueError, struct.error) as error:
            raise ClientError(f'Cannot process local file or gateway response ({type(error).__name__}).') from None
    return wrapped


class GatewayClient:
    def __init__(self, *, root=ROOT, token=None, base_url=None, transport=None):
        self.root = Path(root).resolve()
        values = dotenv_values(self.root / '.env')
        token = token if token is not None else os.environ.get('GATEWAY_TOKEN', values.get('GATEWAY_TOKEN'))
        url = base_url or os.environ.get('GATEWAY_URL') or values.get('GATEWAY_URL') or 'http://127.0.0.1:8001'
        if not token:
            raise ClientError('GATEWAY_TOKEN is missing; configure the project .env.')
        self._http = httpx.Client(base_url=url.rstrip('/'), headers={'Authorization': f'Bearer {token}'},
                                  timeout=httpx.Timeout(600, connect=10), transport=transport)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self._http.close()

    @readable_errors
    def speak(self, text, voice='Mai Anh', out_path=None):
        path = Path(out_path).expanduser().resolve() if out_path else self.root / 'outputs' / (
            datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S') + '-' + uuid4().hex[:12] + '.wav')
        path = path.resolve()  # Resolve default outputs junctions as well as explicit paths.
        if not isinstance(text, str) or not text.strip():
            raise ClientError('Text must not be blank.')
        if len(text) > 20000:
            raise ClientError('Text exceeds the 20000 character limit; shorten the script.')
        protected = {'vieneu-tts', '.venv', 'venv', 'env', 'cli', 'mcp', 'gateway', 'src', 'tests', 'docs', 'voices', 'user_voices', 'cache'}
        relative = path.relative_to(self.root) if path.is_relative_to(self.root) else None
        external_protected = {'vieneu-tts', '.venv', 'venv', 'env', 'voices', 'user_voices', 'site-packages'}
        if (path.suffix.lower() != '.wav'
                or any(p.lower() in external_protected for p in path.parts[:-1])
                or (relative and any(p.lower() in protected for p in relative.parts[:-1]))):
            raise ClientError('Output must be a WAV outside protected source, environment, and voice directories.')
        if path.exists():
            raise ClientError('Output already exists; choose a new output path.')
        path.parent.mkdir(parents=True, exist_ok=True)
        created = False
        try:
            with path.open('xb') as output:
                created = True
                with self._http.stream('POST', '/v1/audio/speech', json={
                    'input': text, 'voice': voice, 'response_format': 'wav'}) as response:
                    response.raise_for_status()
                    for chunk in response.iter_bytes():
                        output.write(chunk)
            size = path.stat().st_size
            with path.open('r+b') as output:
                header = output.read(44)
                if len(header) != 44 or header[:4] != b'RIFF' or header[8:16] != b'WAVEfmt ' or header[36:40] != b'data':
                    raise ClientError('Gateway returned an invalid 44-byte PCM WAV stream.')
                fmt, channels, rate, _, alignment, bits = struct.unpack_from('<HHIIHH', header, 20)
                data_size = size - 44
                if (fmt != 1 or channels < 1 or rate < 1 or bits not in (8, 16, 24, 32)
                        or alignment != channels * (bits // 8) or data_size <= 0 or data_size % alignment
                        or struct.unpack_from('<I', header, 40)[0] not in (0, 0xFFFFFFFF, data_size)):
                    raise ClientError('Gateway returned invalid or truncated PCM WAV audio.')
                output.seek(4)
                output.write(struct.pack('<I', size - 8))
                output.seek(40)
                output.write(struct.pack('<I', size - 44))
            with wave.open(str(path), 'rb') as audio:
                rate = audio.getframerate()
                duration = audio.getnframes() / rate
            return {'path': str(path), 'duration_seconds': duration, 'sample_rate': rate, 'voice': voice}
        except Exception:
            if created:
                path.unlink(missing_ok=True)
            raise

    @readable_errors
    def clone_voice(self, name, audio_path, consent_confirmed, consent_note):
        if consent_confirmed is not True or not isinstance(consent_note, str) or not consent_note.strip():
            raise ClientError('Voice-owner consent must be confirmed with a nonblank consent note.')
        if not name.strip():
            raise ClientError('Voice name must not be blank.')
        clip = Path(audio_path).expanduser().resolve()
        with clip.open('rb') as audio:
            response = self._http.post('/v1/voices', data={
                'name': name, 'denoise': 'true', 'description': 'Created through VieNeu thin client',
                'consent_confirmed': 'true', 'consent_note': consent_note.strip()},
                files={'file': (clip.name, audio, 'application/octet-stream')})
        response.raise_for_status()
        return response.json()

    @readable_errors
    def delete_voice(self, name):
        from urllib.parse import quote
        voices = self.list_voices().get('data', [])
        match = next((voice for voice in voices if voice.get('name') == name or voice.get('id') == name), None)
        if not match or match.get('kind') != 'cloned':
            raise ClientError('Only an existing cloned voice may be deleted; preset voices are protected.')
        response = self._http.delete('/v1/voices/' + quote(name, safe=''))
        response.raise_for_status()
        return response.json() if response.content else {'deleted': True, 'name': name}

    @readable_errors
    def list_voices(self):
        response = self._http.get('/v1/voices')
        response.raise_for_status()
        return response.json()


def main(argv=None):
    import argparse
    import json
    import sys
    parser = argparse.ArgumentParser(description='VieNeu HTTP gateway client (no local model).')
    parser.add_argument('text', nargs='?')
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--file', type=Path, help='UTF-8 script, at most 20000 characters')
    modes.add_argument('--list-voices', action='store_true')
    modes.add_argument('--clone', metavar='NAME', help='Clone only with voice-owner agreement')
    modes.add_argument('--delete-voice', metavar='NAME')
    parser.add_argument('--voice', default='Mai Anh')
    parser.add_argument('-o', '--output', type=Path)
    parser.add_argument('--ref', type=Path)
    parser.add_argument('--consent', action='store_true', help='Confirm that the voice owner agrees to cloning')
    parser.add_argument('--consent-note', default=None)
    args = parser.parse_args(argv)
    if args.text is not None and (args.file or args.list_voices or args.clone or args.delete_voice):
        parser.error('positional text cannot be combined with another operation')
    if not any((args.text is not None, args.file, args.list_voices, args.clone, args.delete_voice)):
        parser.error('provide text, --file, --list-voices, --clone, or --delete-voice')
    if args.clone and (not args.ref or not args.consent):
        parser.error('--clone requires --ref and explicit --consent from the voice owner')
    if not args.clone and (args.ref or args.consent or args.consent_note is not None):
        parser.error('--ref and consent options are only for --clone')
    try:
        with GatewayClient() as client:
            if args.list_voices:
                result = client.list_voices()
            elif args.clone:
                note = args.consent_note if args.consent_note is not None else 'I confirm the voice owner agrees to this voice cloning.'
                result = client.clone_voice(args.clone, args.ref, args.consent, note)
            elif args.delete_voice:
                result = client.delete_voice(args.delete_voice)
            else:
                text = args.file.read_text(encoding='utf-8-sig') if args.file else args.text
                result = client.speak(text, args.voice, args.output)
                print(json.dumps(result, ensure_ascii=False))
                print(result['path'])
                return 0
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (ClientError, OSError, UnicodeError) as error:
        print(f'Error: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
