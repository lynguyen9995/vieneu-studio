# VieNeu Studio (web app)

Browser UI for the local voice stack. Served **by the gateway** at
**http://127.0.0.1:8001/app/** — same origin, same token, same voices as the MCP
server (`mcp/server.py`), the `speak` CLI and any future client.

## Run
1. `start-all.bat` (starts/reuses upstream :8000 and gateway :8001)
2. `open-studio.bat` (or open http://127.0.0.1:8001/app/)

After changing gateway code, restart only the gateway process on :8001
(upstream on :8000 can keep running).

Auto-start at Windows logon (hidden, no admin): `install-autostart.bat` creates
`%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\VieNeu Studio.lnk` →
`wscript autostart\run-hidden.vbs` (waits 15 s, then `start-all.bat` hidden, output in
`logs\autostart.log`). Undo: `uninstall-autostart.bat`. Stop now: `stop-all.bat`.
Restart after code changes: `stop-all.bat`, then `wscript autostart\run-hidden.vbs now`.

## Tabs
| Tab | What it does | API used |
|---|---|---|
| Đọc văn bản | Text / .txt / .pdf → speech, emotion tags `[cười] [thở dài] [hắng giọng]`, advanced sampling | `POST /v1/audio/speech` (pcm) |
| Hội thoại | `Tên: lời` script, one voice per speaker, silence gap, stitched into one WAV | same, one call per line, 3 in parallel |
| Phụ đề SRT | .srt → one WAV, optionally keeping subtitle timing | same, one call per cue |
| Clone giọng | Record mic or upload file → WAV 48 kHz mono in the browser → enroll with consent | `POST /v1/voices` (multipart) |
| Kho giọng | List / preview / delete cloned voices | `GET/DELETE /v1/voices` |
| Lịch sử | Every WAV in `outputs/` (web, CLI and Claude) | `GET /app/api/outputs[/{name}]` |
| Model | Switch engine model: v3 Turbo GPU/CPU/int8, v3 Nano, Custom LoRA | `GET/POST /app/api/engine` |

Smoothness features (all client-side): text cleaning (closing punctuation per line, quotes,
spaces), paragraph split with an adjustable pause, 8 ms fades at every join (no clicks), peak
normalisation to −1 dBFS, live playback while the rest is generated, WAV + MP3 download (lamejs).
Clone tab: try-before-save (`POST /app/api/clone-preview`, reserved engine slot `studio-preview`),
the engine repo's own sample clips (`/app/api/samples`, preview only, cannot be saved).
Parallel requests = engine `max_streams` − 1 (GPU: 3, CPU: 1), leaving room for Claude/CLI.

Every generated result is also saved to `outputs/` (`POST /app/api/outputs`), so the
user, the CLI and Claude share one history.

## Engine facts (VieNeu v3 Turbo)
- Honored: `voice`, `response_format` (pcm|wav), `sample_rate` (48000 default), `temperature`,
  `top_k`, `top_p`, `repetition_penalty`, `max_chars`.
- **`speed` is accepted but ignored** by the engine — the UI does not show a speed control.
- Cloning needs only a 3–8 s clip; v3 does not use a reference transcript.

## Security model (localhost, single user)
- Public without token: only `/app/`, `/app/app.js`, `/app/style.css`, `/health`.
  The page is served only for Host `127.0.0.1:8001` / `localhost:8001` (DNS-rebinding guard).
- The page embeds the gateway token in a `<meta>` tag at serve time (never stored in files).
  A foreign website cannot read it: no CORS grant, Host check, `frame-ancestors 'none'`, CSP.
- `/app/api/*` and `/v1/*` still require `Authorization: Bearer <token>`.
- Never expose port 8000 or 8001 beyond localhost.

## Adding another client (e.g. YouTube narration extension)
- Call `http://127.0.0.1:8001/v1/audio/speech` with `Authorization: Bearer <token from .env>`.
- Add the client's origin to `ALLOWED_ORIGINS` in `.env` (e.g. `chrome-extension://<id>`),
  restart the gateway. No gateway code change needed.
