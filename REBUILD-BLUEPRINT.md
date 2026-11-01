# Voice Studio — Rebuild Blueprint

> **Cho người dùng (tiếng Việt):** Khi cài lại máy, chuyển lên VPS, hoặc muốn dùng một repo giọng nói KHÁC,
> mở AI agent (Claude Code / Codex / …), dán **Prompt mẫu** ở cuối file này kèm link repo giọng nói
> và đính kèm (hoặc dán) chính file này. AI sẽ tự dựng lại toàn bộ hệ thống giống hệt bản đang chạy.
> Mọi thứ phía trên (web app, MCP cho Claude, lệnh `speak`, extension sau này) dùng chung một
> "hợp đồng API", nên đổi engine giọng nói không phải làm lại chúng.

Everything below is written for the AI agent doing the rebuild.

---

## 0. Owner's intent (non-negotiable goals)

1. One **local voice service** that both a human (browser UI) and AI agents (MCP, CLI) use.
2. **Engine-agnostic**: any TTS repo (VieNeu-TTS today, something else tomorrow) is plugged in
   behind the same contract. Clients never talk to an engine directly.
3. Cloned voices are **persistent** (survive restarts) and **shared** by every client.
4. Easy to extend with new clients (YouTube narration Chrome extension, Telegram bot, news-reading
   scheduled jobs, local LLM agents) without touching the engine or the other clients.
5. Safe by default: loopback only, bearer token, explicit CORS origins, consent before cloning.
6. Communicate with the owner in **Vietnamese**; English is fine for code, docs and agent prompts.
7. Owner's machine (reference): Windows, Lenovo Legion 5 Pro, RTX 3060 Laptop 6 GB VRAM, workspace
   `C:\AI-Workspace\Projects`. May later run on a Linux VPS (CPU or GPU).

## 1. Architecture

```
                       ┌──────────── clients (never import a model) ─────────────┐
 Browser studio /app ──┤ MCP server (stdio) → Claude / Codex / local LLM agents   │
 CLI `speak`         ──┤ future: Chrome extension, bots, scheduled news jobs      │
                       └────────────────────────────┬─────────────────────────────┘
                                                    │ HTTP, Bearer token
                          GATEWAY  127.0.0.1:8001  (the CONTRACT, section 2)
                          auth · CORS · persistent voice store · outputs · static UI
                                                    │ HTTP (engine adapter, section 3)
                          ENGINE   127.0.0.1:8000  (one TTS repo, holds the GPU model)
```

Project layout (`<root>` = `C:\AI-Workspace\Projects\vieneu-studio` or `/opt/voice-studio`):

```
<root>/
  <EngineRepo>/          upstream clone, NEVER modified (git pull to update)
  gateway/               app.py (contract) · webapp.py (/app + outputs + model switch + clone preview)
                         store.py · config.py · engine.py (model options, engine.json, start/stop) · launch.py · run.py
  engine/                <engine>_adapter.py — runs the engine's server with the chosen model (never edits upstream)
  engine.json            persisted model choice, e.g. {"preset":"turbo-gpu","lora_path":""}
  webapp/                index.html · app.js · style.css  (plain JS, no build step)
  cli/speak.py           CLI client
  mcp/server.py          MCP stdio server (official Python MCP SDK), NO mcp/__init__.py
  voices/                persistent clones: <slug>.wav + <slug>.json (+ .presets, .deleted)
  outputs/               every generated WAV (web, CLI, MCP share one history)
  tests/                 pytest (mock the engine; never load a model in tests)
  docs/                  API.md · GATEWAY.md · MCP-SETUP.md · INSTALLATION.md
  .env                   GATEWAY_TOKEN (random, generated on first run, gitignored), ALLOWED_ORIGINS, FFMPEG_PATH
  start-server.(bat|sh)  start engine only · start-all.(bat|sh) engine+gateway · open-studio.(bat|sh)
  REBUILD-BLUEPRINT.md   this file
```

## 2. The Voice Service Contract (gateway :8001) — keep this stable forever

Auth: every route except `/health` and the static studio files requires
`Authorization: Bearer <GATEWAY_TOKEN>`. Bind `127.0.0.1` only. CORS: only exact origins from
`ALLOWED_ORIGINS` plus the gateway's own `http://127.0.0.1:8001` / `http://localhost:8001`; never `*`.

| Route | Purpose | Shape |
|---|---|---|
| `GET /health` | liveness (public) | `{"status":"ok","backend":"pytorch|onnx|...","service":"vieneu-gateway"}` |
| `GET /v1/models` | engine model ids | OpenAI-style list |
| `GET /v1/voices` | all voices | `{"object":"list","data":[{"id","name","description","gender?","featured?","aliases?","kind":"preset|cloned"}]}`. Preset description format: `"<Giới tính> · <Miền> · <Phong cách>"` when the engine has that info |
| `POST /v1/voices` | clone (multipart) | fields `name`, `file` (wav/mp3/flac/ogg/m4a, ≤20 MB, ≤30 s), `denoise`=true, `description`, `consent_confirmed`=true, `consent_note` (non-empty). 403 without consent, 409 on preset/existing name. Saves to `voices/` then enrolls into the engine |
| `DELETE /v1/voices/{name}` | delete a clone | presets → 409 |
| `POST /v1/audio/speech` | synthesize (OpenAI TTS compatible) | JSON `{model, input ≤20000 chars, voice, response_format:"pcm"|"wav", sample_rate, + engine sampling knobs}`; streams audio; header `X-Sample-Rate`; PCM = s16le mono |
| `GET /app/` + `/app/app.js`, `/app/style.css` | browser studio (public, loopback Host only, token injected into `<meta name="vieneu-token">`) | HTML |
| `POST /v1/translate` | translate lines via Ollama `hy-mt2` (grouped, order kept) | `{texts, target:"vi", group}` → `{translations}` |
| `GET /v1/translate/status` | is the translator ready | `{ok, model, error?}` |
| `GET /v1/listen/status` | speech-to-text service health (8002) | `{status, service:"vieneu-stt", loaded, device}` |
| `POST /v1/listen/prepare` | listen ahead: download audio (yt-dlp) + Whisper, YouTube id only | `{video_id}` → job |
| `GET /v1/listen/jobs/{id}` | progressive segments of a prepared video | `?since=N` → `{state, segments, next}` |
| `POST /v1/audio/transcriptions` | OpenAI-compatible transcription of one audio file/chunk | multipart `file` → `{text}` |
| `POST /v1/pair` | token for the listed browser extension only (no token needed, Origin-checked) | `{token}` |
| `GET/POST /app/api/engine` | list model options / switch model (restart engine, re-snapshot presets, replay clones) | `{preset, lora_path}` |
| `POST /app/api/clone-preview` | try a clip before saving (reserved engine slot `studio-preview`) | multipart `file,text,denoise` → PCM + `X-Sample-Rate` |
| `GET /app/api/samples[/{name}]` | engine repo's demo clips, preview only | |
| `GET /app/api/outputs` | history | `{"data":[{name,size,modified,duration_seconds,sample_rate,source}]}` |
| `GET /app/api/outputs/{name}` | download one WAV | audio/wav |
| `POST /app/api/outputs?label=` | save a WAV produced by a client | raw PCM16 WAV body → `{name,path,duration_seconds,sample_rate}` |

Gateway behaviors: on startup wait for engine `/health`, snapshot preset list into
`voices/.presets`, then **re-enroll every stored clone** into the engine (engines usually keep
clones in RAM only). Stream audio through without buffering. Convert uploads to mono PCM16 WAV
with ffmpeg. Never log or print the token.

MCP tools (stdio, call the gateway, never import the model, never return audio bytes):
`speak(text, voice, out_path?) → {path, duration_seconds, sample_rate, voice}`,
`list_voices()`, `clone_voice(name, audio_path, consent_confirmed, consent_note)` (refuse without
consent), `delete_voice(name)` (clones only). Errors are readable: "start start-all".

## 3. Plugging in an engine (any TTS repo)

Read the repo's README, API/server code and license first. Then pick ONE path:

**A. Repo ships an OpenAI-compatible server** (`POST /v1/audio/speech`, ideally `/v1/voices`)
→ run it on 127.0.0.1:8000 and let the gateway proxy it. This is the VieNeu path
(`uv run python -m apps.openai_speech`).

**B. Repo only has a Python SDK / CLI / its own web UI**
→ write `gateway/adapters/<engine>_server.py`: a tiny FastAPI app on 127.0.0.1:8000 that loads the
model ONCE and implements `/health`, `/v1/models`, `/v1/voices` (GET/POST), `/v1/audio/speech`
(pcm + wav, `X-Sample-Rate`) by calling the SDK. Run it inside the engine repo's own environment.
The gateway, web app, CLI and MCP stay unchanged.

**C. Repo ships its own web UI you also want** (Gradio etc.)
→ keep it as an optional extra on its own loopback port (VieNeu: `uv run vieneu-web` → :7860) and link
it from the studio. Its clones live only inside it; the shared, persistent path is always the gateway.

For every engine, record in `docs/API.md`: which request params the engine really honors (VieNeu v3
Turbo honors voice, response_format, sample_rate 48000/24000/16000/8000, temperature, top_k, top_p,
repetition_penalty, max_chars; it IGNORES `speed`), whether cloning needs a transcript (VieNeu v3: no),
native sample rate, VRAM use and latency (measure with 1 and 4 concurrent requests). The studio must
not show controls the engine ignores.

## 4. Engine reference: VieNeu-TTS (https://github.com/pnnbao97/VieNeu-TTS)
- Apache-2.0 code; v3 Turbo is the open model (v4 is API-only). 25 presets, 48 kHz, clone from 3–8 s.
- Install: `uv sync --extra cuda` (GPU, torch 2.8 + cu128) or `uv sync` (CPU/ONNX, torch-free).
- Engine server env: `HOST=127.0.0.1 PORT=8000 VIENEU_MAX_STREAMS=4 VIENEU_BACKEND=pytorch
  VIENEU_DEVICE=cuda UV_NO_SYNC=1 HF_HOME=<root>/cache/huggingface`.
- Measured on RTX 3060 Laptop 6 GB: ~0.9 GB VRAM for the server, first audio ~235–310 ms, RTF ~0.65.
- Studio stitches multi-line jobs (conversation, SRT) client-side with (max_streams − 1) parallel requests.
- Model options exposed: v3 Turbo GPU (default, best), Turbo CPU fp32, Turbo CPU int8 (needs VNNI),
  v3 Nano CPU (preview, 24 kHz → adapter resamples to 48 kHz, adds alias table), Custom LoRA (merged
  folder from finetune/). The upstream API server hardcodes v3turbo, so engine/vieneu_adapter.py swaps the
  module-level `Vieneu` factory. v1/v2 are deliberately NOT offered (deprecated, need `legacy` extra).
- Owner's quality defaults: clean text + closing punctuation, 0.5 s paragraph pause, edge fades,
  −1 dBFS peak normalisation, live playback on, WAV + MP3 export.

## 4b. Translator (local, for the narration extension)
- Ollama on Windows (`irm https://ollama.com/install.ps1 | iex`, no admin, 127.0.0.1:11434).
- Model: **Tencent Hy-MT2-1.8B** (May 2026, Apache-2.0, 33 languages incl. vi/ko/en/zh/ja, ~1.1 GB Q4).
  Do NOT use HY-MT1.5: its licence excludes the EU, UK and South Korea (owner lives in Korea).
  Do not use the -2bit / -1.25bit repos (need a custom llama.cpp kernel).
- `ollama pull hf.co/tencent/Hy-MT2-1.8B-GGUF` then `ollama create hy-mt2 -f models/hy-mt2.Modelfile`.
  The Modelfile is REQUIRED: Ollama's auto-converted template drops the prompt and the model prints
  garbage. It rebuilds the GGUF's own template `<bos>[system<no3>]<｜hy_User｜>text<｜hy_Assistant｜>`,
  stops on `<｜hy_place▁holder▁no▁2｜>`, temp 0.7 / top_p 0.6 / top_k 20 / repeat_penalty 1.05, num_ctx 4096.
- Prompt: "Translate the following text into Vietnamese, only output the translated result without
  additional explanation.\n\n<text>". Verified 2026-11-01: EN→VI and KO→VI correct and fluent; KO→VI
  occasionally adds a short phrase not in the source (watch for this when fitting narration length).
- Lines go in groups of 6 (context keeps pronouns consistent). A group answer is rejected and that group is
  re-done line by line when the line count differs, when a line repeats although the inputs differ (the model
  merged two lines and repeated one to keep the count, heard as "the same sentence read twice"), or when a
  line is > 4×source+20 or < 0.15×source characters.
- Extension side of the same symptom: ignore seeks < 1.5 s (YouTube seeks by itself on quality switch /
  buffering) and drop roll-up caption lines already shown in the previous event.

## 4d. Listening service (speech-to-text, `stt/`, 127.0.0.1:8002) — details in docs/LISTEN.md
- Separate process started by the launcher as an OPTIONAL service (failure never blocks engine/gateway);
  `stop-all` stops it too (only when `/health` says `service: vieneu-stt`).
- faster-whisper `large-v3-turbo`, int8_float16 on CUDA, CPU int8 fallback; lazy load, unload after 10 idle min.
- cuBLAS from the `nvidia-cublas-cu12` wheel (DLL dirs added at runtime on Windows). cuDNN not needed (CTranslate2 ≥ 4.6.3).
- Listen ahead: validated 11-char YouTube id → yt-dlp audio-only (needs Deno) → segments published progressively
  → audio deleted; text cache capped 50 MB; refuse when < 2 GB free or > 3 h.
- Live chunks: greedy decoding (beam 1), VAD on. Both use `condition_on_previous_text=False` (fewer loops).

## 4c. Narration extension (separate project `C:\AI-Workspace\Projects\thuyet-minh`)
- Chrome MV3, fixed extension ID via manifest `key` (keep `key.pem` out of git); its origin is in `ALLOWED_ORIGINS`.
- Only the service worker talks to the gateway (token never reaches web pages); auto-pairs with `POST /v1/pair` (Chrome omits Origin on GET from a service worker).
- YouTube captions: list tracks in the page world; YouTube rejects direct timedtext downloads without its
  proof-of-origin token, so the page-world hook captures the player's own `/api/timedtext` responses after
  switching the track on. Then: merge into sentences → `/v1/translate` → `/v1/audio/speech` (pcm 24 kHz)
  → play with `<audio>` (pitch-preserving rate up to ~1.3×) in sync with the video; duck original audio.
- Listen mode (no captions): tab audio → Whisper `/v1/audio/transcriptions` (planned).

## 5. Platform notes

**Windows (reference machine):** `uv` at `%USERPROFILE%\.local\bin\uv.exe` (may not be on PATH — call it
by full path). PowerShell has no `&&`; give users one command per line. `.bat` launchers. Claude Desktop
config lives in the app's own data folder — tell the user to use *Settings → Developer → Edit Config*
and merge an `mcpServers.voice` entry (command = uv.exe, args = `--directory <root> run --no-sync python
<root>/mcp/server.py`); never overwrite their existing keys.
Owner wants the stack **always on**: provide `install-autostart.bat` (Startup-folder shortcut → hidden
`autostart/run-hidden.vbs` → `start-all.bat`, 15 s logon delay, log to `logs/autostart.log`; no admin,
no Task Scheduler), `uninstall-autostart.bat`, and `stop-all.bat`. On Linux use systemd units instead.

**Linux VPS:** same layout under `/opt/voice-studio`, `.sh` launchers, two `systemd` units
(engine, gateway) with `Restart=on-failure`. Keep both bound to 127.0.0.1. Reach the studio through an
SSH tunnel (`ssh -L 8001:127.0.0.1:8001 vps`) — do NOT open ports publicly. If public access is ever
required: reverse proxy with TLS + its own login in front, add the proxy origin to `ALLOWED_ORIGINS`,
and ask the owner first. CPU-only VPS: use the engine's CPU path and expect slower synthesis.
Remote MCP: run the MCP server on the machine where the AI client runs and point it at the tunnel.

## 6. Rebuild procedure (agent checklist)

1. Discover first: OS, GPU (`nvidia-smi`), disk, existing `<root>`. If `<root>` or a backup/repo of it
   exists, **reuse its code** (gateway/, webapp/, cli/, mcp/, tests/, docs/) and its `voices/`; do not rewrite.
2. Clone the engine repo into `<root>/<EngineRepo>`; install per its README; never modify it.
3. Choose path A/B/C (section 3); start the engine on 127.0.0.1:8000; verify `/health` and one WAV.
4. Create/restore the gateway, studio, CLI, MCP to the contract in section 2. Generate `.env` token.
5. Tests with a mocked engine (auth, CORS allow/deny, Host guard, clone persistence + replay, consent,
   streaming passthrough, outputs save/list/traversal). All must pass.
6. Manual check: start-all → open `/app/` → synthesize a preset → file appears in Lịch sử; `speak`
   CLI works; MCP lists its 4 tools. Report numbers from file metadata, never "I listened".
7. Write the MCP config snippet for the owner's AI clients; do not edit global configs yourself.
8. Final report in Vietnamese: what was installed, URLs, how to start/stop, measured latency/VRAM,
   anything that deviates from this blueprint.

## 7. Safety rules for the agent
- Loopback only; never expose 8000/8001; never wildcard CORS; never print the token.
- Never clone a real person's voice without that person's consent; tests use synthetic audio.
- No firewall/system changes, no global installs beyond what's required (ask first: ffmpeg, node).
- Don't delete user data (`voices/`, `outputs/`); back up a file before changing it.
- No commits/pushes unless asked. Stop and ask when a step fails twice or a choice is irreversible.

## 8. Tip for the owner
Keep `<root>` (without `.env`, `cache/`, `outputs/`, engine repo) in a **private git repo**. Then a
rebuild = clone that repo + this blueprint + the engine link, and the AI only re-installs the engine.

---

## Prompt mẫu (copy nguyên khối, thay phần trong < >)

```
Read the attached REBUILD-BLUEPRINT.md fully and follow it exactly.
Engine repo to use: <https://github.com/pnnbao97/VieNeu-TTS  — hoặc link repo giọng nói khác>
Target machine: <Windows laptop | Linux VPS (CPU/GPU)>   Root folder: <C:\AI-Workspace\Projects\vieneu-studio | /opt/voice-studio>
Existing code to reuse (if any): <link private repo vieneu-studio, hoặc "none">
Do discovery first, pick engine path A/B/C from section 3, keep the Voice Service Contract in
section 2 unchanged so the web studio, MCP, CLI and future clients keep working.
Report back to me in Vietnamese with the checklist in section 6.
```
