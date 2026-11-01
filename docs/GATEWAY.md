# Local gateway (step 2)

The upstream model remains at `127.0.0.1:8000`, unchanged. The model-free adapter listens only at **127.0.0.1:8001**, one worker. It has its own `.venv`, `pyproject.toml` and `uv.lock` at workspace root. It does not import torch/vieneu or load another model.

## Start / stop

Run `C:\AI-Workspace\Projects\vieneu-studio\start-all.bat` from Command Prompt or double-click it. It reuses a healthy upstream if already running; otherwise starts the original `start-server.bat`, waits for health, then starts the gateway. Keep the launcher window open. **Ctrl+C stops only child processes started by that launcher. Reused services remain running** and must be stopped in their original console. No Windows auto-start, firewall rule or global application configuration is installed. Logs append to `logs/step2-upstream.log` and `logs/step2-gateway.log`.

A second start-all call that finds both expected healthy services prints that it reused them and exits without spawning duplicates. An unrecognized/unhealthy service owning a port is not killed/replaced. During this step, the existing step-1 upstream instance was reused.

Direct gateway-only command (from workspace root):

```bat
"%USERPROFILE%\.local\bin\uv.exe" run --no-sync python -m gateway.run
```

To update only the adapter's dependencies, stop gateway and run root `uv sync --locked`. Do not run dependency commands inside `VieNeu-TTS` for the adapter.

## Token and origins

First gateway start creates **`vieneu-studio\.env`** with a cryptographically random token. The token is never displayed; read it locally if configuring your own client. The file and all voice/output/log/cache data are gitignored. Do not paste it into chat, source files, screenshots, MCP configs or logs. CLI/MCP load it automatically. Standard same-user local filesystem access still applies; this is not an encrypted secret vault or a sandbox against other programs running as your Windows user.

Every gateway endpoint except `/health` requires `Authorization: Bearer <token>`, including unknown routes. Built-in Swagger/OpenAPI UI routes are disabled. OPTIONS **CORS preflight** is the necessary exception: the browser sends no bearer token at preflight time, so only an allowlisted origin/method/header combination receives a preflight response. The actual request still requires the token.

`ALLOWED_ORIGINS` defaults empty. To allow future clients, edit only this nonsecret line in `.env`, then restart the gateway:

```text
ALLOWED_ORIGINS=chrome-extension://YOUR_EXTENSION_ID,http://localhost:3210
```

Exact origins only, no wildcard/trailing slash/path. A different hostname/port is a different origin. Non-browser CLI/MCP requests normally have no Origin and need only bearer auth. Disallowed origins are denied (403 on actual calls, unsuccessful preflight without allow-origin). Allowlisted calls expose X-Sample-Rate, X-Request-Id and X-VieNeu-Ignored. No cookies/credentialed CORS is enabled.

**Boundary:** upstream port8000 remains unauthenticated and has no CORS, as originally installed; same-machine programs can bypass the adapter by calling8000. The gateway's auth is not a firewall protecting upstream. Keep both listeners loopback-only; do not publish them or grant public-network firewall permissions.

## Routes

- GET `/health`: upstream health plus `service: "vieneu-gateway"`, public.
- GET `/v1/models`: upstream JSON, authenticated.
- GET `/v1/voices`: approved preset catalog plus persisted clones; adds `kind: "preset" | "cloned"`. Unmanaged residual custom voices in upstream RAM are not advertised.
- POST `/v1/audio/speech`: same JSON/PCM/WAV/SSE contract as `API.md`. Success bytes stream incrementally; selected audio/error headers and upstream error status are retained. No audio cache or whole-response buffering. WAV length fields remain unknown during streaming; the CLI finalizes the saved file afterward.
- POST `/v1/voices`: authenticated multipart `name`, `file`, optional `denoise` (true), `description` (source description). **Also requires `consent_confirmed=true` and a nonempty `consent_note`** stating that the voice owner agreed. Owner consent is caller attestation, not independently verified by software. Preset names/aliases cannot be replaced. Gateway also rejects existing stored names with409; explicitly delete before replacing. This is intentionally stricter than upstream's custom-name overwrite behavior.
- DELETE `/v1/voices/{URL-encoded name}`: stored clones only, presets/aliases409, unknown404; repeated deletion of a tombstoned clone is idempotent200, invalid name400. Removes its managed WAV/JSON. Response says `upstream_removed: false` because upstream exposes no deletion route. Persistent private tombstones and the approved catalog keep deleted/unknown names unavailable through gateway speech/listing until/after restarts. Upstream RAM is not erased until upstream restart.

No endpoint returns consent notes or local source audio. No remote URL/file fetch or client-specified filesystem write path is accepted by gateway uploads.

## Persistence and conversion

After confirmed owner consent, upload bytes are decoded/converted to mono PCM16 WAV with the already-installed ffmpeg, then enrolled upstream and saved to `voices/<slug>.wav` plus matching `.json`. Metadata includes name, UTC created_at, source_description, consent_confirmed, consent_note and denoise. JSON is the completion marker; startup refuses malformed/incomplete records instead of silently forgetting them. Files are saved only after successful upstream enrollment; a disk-write failure is reported, not claimed as persisted. Metadata/state publication uses exclusively created unpredictable temporary files and atomic replace. A pending tombstone precedes voice publication and removes interrupted voice artifacts on next startup. Existing junctions/reparse points and linked state files are rejected; this is not protection against an actively hostile same-user process racing filesystem operations. A failed write can leave an unused `.write-*` temporary file after process termination; it is never treated as a voice record.

Startup waits for upstream healthy, loads every committed consented record and re-registers it. This is **gateway-startup replay**, not continuous monitoring: if upstream alone restarts while gateway stays running, restart gateway to replay the clones. Four-stream GPU limit stays in upstream. Conversion and enrollment are reference-only, no second model loaded by the adapter.

Recognized input formats WAV/MP3/FLAC/OGG/M4A. ffmpeg is invoked without shell interpolation, forced format and pipe-only protocol; conversion timeout30 seconds. Uploaded limit20 MiB; converted reference at most30 seconds, recommend3–8 seconds. Without ffmpeg, only valid mono PCM16 WAV is accepted; other formats report the missing decoder. No automatic global install occurs. `FFMPEG_PATH` can specify an existing executable if external terminals lack Hermes's ffmpeg PATH.

Private `voices/.presets` captures the original approved catalog so gateway-only restarts cannot mistake deleted custom upstream RAM entries for presets. `voices/.deleted` stores tombstones. Do not edit these while running. After deliberately updating model presets, review/catalog migration is manual; do not delete voice data blindly.

## Testing safely

No real speaker was enrolled in this step. Persistence/replay/delete testing uses mock upstream plus generated nonvoice PCM. M4A conversion uses a generated test signal with the existing ffmpeg. For a real consented test later, use only an owner-provided clip and explicit consent, call CLI clone, restart gateway/upstream, list voices and synthesize, then delete through gateway. Do not use Mai Anh/preset output as a cloning fixture.

Run from root:

```bat
"%USERPROFILE%\.local\bin\uv.exe" run --no-sync pytest tests -q
```

Tests may use isolated temporary directories; production voice files and real .env are not test fixtures. The installed Starlette emits a known httpx TestClient deprecation warning; this is not a failed assertion and no optional httpx2 was installed merely to hide it.

## Step 3 additions — web studio (additive, backward compatible)
- `gateway/webapp.py` mounts the browser studio at `/app/` (see `webapp/README.md`).
- Public routes added: `/app`, `/app/`, `/app/app.js`, `/app/style.css` — only for Host
  `127.0.0.1:8001`/`localhost:8001`; anything else under `/app` → 403.
- Token-protected routes added: `GET /app/api/outputs`, `GET /app/api/outputs/{name}`,
  `POST /app/api/outputs?label=` (raw PCM16 WAV body, ≤300 MB, saved as
  `outputs/<ts>-web-<label>-<rand>.wav`, never overwrites).
- The gateway's own origins (`http://127.0.0.1:8001`, `http://localhost:8001`) are always
  accepted in addition to `ALLOWED_ORIGINS`.
- `create_app(..., web_hosts=...)` lets tests override the loopback Host allowlist.
- Tests: `tests/test_webapp.py`. Backup of the pre-step-3 file: `logs/app.py.before-step3.bak`.

## Step 3b — model selection, clone preview, samples
- `engine/vieneu_adapter.py` runs upstream `apps.openai_speech` inside the upstream env with a swapped
  `Vieneu` factory (STUDIO_MODE=v3turbo|v3nano, STUDIO_BACKBONE_REPO=LoRA folder). v3 Nano is wrapped
  to 48 kHz output + alias table so the unchanged server works. Upstream files stay untouched.
- `gateway/engine.py`: options, `engine.json` (persisted choice), env/command builder, port→PID
  (netstat on Windows, ss on Linux), kill, detached spawn (logs/engine.log). CLI: `python -m gateway.engine run|stop|status`.
- `start-server.bat` now runs `gateway.engine run` (honours engine.json); `stop-all.bat` stops 8001 + 8000.
- `launch.py` no longer tears everything down when the engine child exits (a model switch replaces it).
- Gateway routes: `GET/POST /app/api/engine` (switch = verify 8000 is our engine via /health → kill →
  spawn → wait health → re-snapshot presets → replay clones), `POST /app/api/clone-preview`,
  `GET /app/api/samples[/{name}]`. Name `studio-preview` is reserved (409 on clone, 400 on speech).
- `/health` now includes `engine: {preset,label,state,message,warnings}`.
- v1/v2 (neucodec/LMDeploy family) deliberately not offered: deprecated upstream, need the `legacy`
  extra and a different adapter (24 kHz, transcript-based cloning).

## Step 4a — translation + extension pairing (for thuyet-minh)
- `gateway/translate.py`: `Translator` → Ollama on loopback (`OLLAMA_URL`, default http://127.0.0.1:11434;
  `TRANSLATE_MODEL`, default `hy-mt2`). One request at a time. Lines go in groups (default 6) with an
  "exactly N lines" instruction for context; a group that does not come back with N lines is redone line by line.
- `POST /v1/translate` `{texts: [...≤300], target: "vi", group: 1-12}` → `{translations, model, target}`;
  503 with a readable message when Ollama/model is missing. `GET /v1/translate/status` → `{ok, model, error?}`.
- `POST /v1/pair` (no token; POST because Chrome omits Origin on GET from an extension service worker): returns `{token}` only when the request's Origin is a `chrome-extension://…`
  listed in `ALLOWED_ORIGINS`. Browsers set Origin, so websites and other extensions cannot obtain it.
- `.env` now lists the thuyet-minh extension origin (fixed ID from the manifest `key`).
- Tests: `tests/test_translate.py` (mocked Ollama).
