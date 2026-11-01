# Listening (speech-to-text) — `stt/` on 127.0.0.1:8002

Used by the narration extension when a video has **no captions**, and by any client that wants
OpenAI-style transcription (`POST /v1/audio/transcriptions` on the gateway).

## How it works

```
extension ──token──▶ gateway :8001 ──▶ stt :8002 (loopback only, no token, like the engine on :8000)
                                         ├─ Recognizer: faster-whisper large-v3-turbo, int8_float16 on CUDA
                                         │   (CPU int8 fallback). Loaded on first use, unloaded after 10 idle
                                         │   minutes so the VRAM goes back to VieNeu + hy-mt2.
                                         └─ Preparer ("listen ahead", YouTube): yt-dlp downloads the AUDIO ONLY
                                             into cache/stt-tmp, Whisper publishes segments as it goes, the audio
                                             file is deleted at the end. Only the text is kept (cache/stt, max 50 MB,
                                             oldest removed first). Guards: < 2 GB free disk or > 3 h video → refused.
```

VRAM on the RTX 3060 Laptop 6 GB: VieNeu ~0.9 GB + hy-mt2 ~1.5 GB + Whisper turbo int8 ~1.5 GB.

## Install (once, Windows, in `C:\AI-Workspace\Projects\vieneu-studio`)

```powershell
& "$env:USERPROFILE\.local\bin\uv.exe" sync                       # faster-whisper, yt-dlp (~150 MB)
winget install --id DenoLand.Deno                                  # yt-dlp needs a JavaScript runtime for YouTube
& "$env:USERPROFILE\.local\bin\uv.exe" run --no-sync python -m stt.server --download   # model ~1.6 GB, prints "OK: … on cuda"
```

Then restart the services (`stop-all.bat`, then `wscript autostart\run-hidden.vbs now`). The launcher starts
the listening service as an **optional** third service: if it fails, narration from captions keeps working
(see `logs/stt.log`).

cuBLAS on Windows: taken from `VieNeu-TTS/.venv/Lib/site-packages/torch/lib` (the voice engine's PyTorch).
Without it Whisper falls back to CPU int8 (`/v1/listen/status` shows `device: cpu`). On Linux the
`nvidia-cublas-cu12` wheel is installed and preloaded.

## API

Gateway (Bearer token, extension origin allowed by CORS):

| Route | Body / query | Result |
|---|---|---|
| `GET /v1/listen/status` | | `{status, service:"vieneu-stt", model, loaded, device, error}` |
| `POST /v1/listen/prepare` | `{"video_id": "<11-char YouTube id>"}` | job view (state `queued`) |
| `GET /v1/listen/jobs/{video_id}?since=N` | | `{state, language, duration, progress, segments[N:], next, code, error}` |
| `POST /v1/audio/transcriptions` | multipart `file` (+ `language`, `response_format` json/verbose_json/text) | `{text}` / segments |

Job states: `queued → downloading → transcribing → done`, or `error` with `code`
(`low_disk`, `too_long`, `live`, `download_failed`, `not_installed`, `model_failed`, `failed`). An errored job
can be prepared again.

## Settings (`.env`, optional)

`STT_MODEL` (default `large-v3-turbo`), `STT_DEVICE` (`auto`/`cuda`/`cpu`), `STT_IDLE_SECONDS` (default 600),
`STT_URL` (default `http://127.0.0.1:8002`, loopback only).

## Keeping yt-dlp working

YouTube changes often. If "listen ahead" starts failing with `download_failed`:
`uv lock --upgrade-package yt-dlp` then `uv sync`, and restart. Live listening (phase 3) does not depend on it.
