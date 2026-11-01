# VieNeu Studio — local v3 Turbo HTTP service

Workspace: `C:\AI-Workspace\Projects\vieneu-studio`

This workspace contains unmodified upstream [VieNeu-TTS](https://github.com/pnnbao97/VieNeu-TTS), a lightweight authenticated gateway, a CLI, and a model-free stdio MCP adapter. No desktop app/Chrome extension/custom UI or Windows auto-start is built. Real synthesis tests use only built-in presets; cloning tests use generated signals and a mock upstream.

## Step 2 — recommended entry point

Run `C:\AI-Workspace\Projects\vieneu-studio\start-all.bat`. It starts/reuses upstream **127.0.0.1:8000**, then the authenticated streaming gateway **127.0.0.1:8001**. Keep its window open; Ctrl+C stops only processes it started, not reused services. Gateway token is generated in `.env` (gitignored; never print/share it). `ALLOWED_ORIGINS` defaults empty; add explicit extension/app origins and restart gateway when needed. Full security, persistence and stop details: [docs/GATEWAY.md](docs/GATEWAY.md).

CLI from this workspace:

```bat
"%USERPROFILE%\.local\bin\uv.exe" run --no-sync python cli\speak.py "Xin chào" --voice "Mai Anh"
"%USERPROFILE%\.local\bin\uv.exe" run --no-sync python cli\speak.py --list-voices
```

Default files go to `outputs\`; the final synthesis stdout line is the absolute output path. Gateway-managed consented clones persist in `voices\<slug>.wav` + `.json`; original upstream alone still has RAM-only enrollment. DELETE removes local managed files and blocks residual upstream RAM names through gateway, but upstream itself has no delete API. Auth/CORS apply to8001 only; same-machine processes can still call unauthenticated8000 directly. MCP setup (no global configs changed): [docs/MCP-SETUP.md](docs/MCP-SETUP.md).

The remaining sections describe the original upstream-only installation and its port8000 behavior.

## Start

In **Command Prompt** (or double-click the batch file):

```bat
C:\AI-Workspace\Projects\vieneu-studio\start-server.bat
```

Wait for the model download (first start only), warm-up and `Application startup complete`. API base URL: `http://127.0.0.1:8000`. The batch file changes into the clone automatically. It explicitly sets `HOST=127.0.0.1`, `PORT=8000`, `VIENEU_MAX_STREAMS=4`, `VIENEU_BACKEND=pytorch` and `VIENEU_DEVICE=cuda`. GPU mode is explicit so a later missing CUDA dependency cannot silently turn this tested launcher into a CPU service.

The launcher's underlying command is:

```bat
"%USERPROFILE%\.local\bin\uv.exe" run python -m apps.openai_speech
```

The batch file sets `UV_NO_SYNC=1` (equivalent to `uv run --no-sync ...`) to use the already verified CUDA environment and avoid unintentional dependency changes. Run the explicit CUDA sync below for installation/updates, not on every launch. `UV_PYTHON_DOWNLOADS=never` prevents runtime interpreter provisioning. uv was installed using the official Windows README command with `UV_NO_MODIFY_PATH=1`, so PATH/system settings were not modified; scripts use its absolute per-user location.

Only one server can own port 8000. If it is already running, use it or stop its own console; do not terminate an unrelated process. Do not set `HOST=0.0.0.0` or open firewall ports. If Windows requests firewall permission, **do not allow public networks**; cancel and resolve it with the machine owner.

## Stop

Press **Ctrl+C in the console running `start-server.bat`** and wait for shutdown. If cmd asks `Terminate batch job (Y/N)?`, enter `Y`. Closing that console also ends its attached server. No background Windows service or scheduled task was installed.

If Hermes launched the current instance in its managed terminal, stop that specific terminal/process from Hermes. This is a manually started process, not an auto-restarting Windows service.

## Test

Health, model list, voice list:

```bat
curl.exe http://127.0.0.1:8000/health
curl.exe http://127.0.0.1:8000/v1/models
curl.exe http://127.0.0.1:8000/v1/voices
```

A UTF-8 `test-request.json` is produced by the verification script (preset **Mai Anh**). From this workspace, one synthesis command is:

```bat
curl.exe --fail-with-body --no-buffer http://127.0.0.1:8000/v1/audio/speech -H "Content-Type: application/json" --data-binary "@test-request.json" --output curl-test.wav
```

The API returns a streaming WAV with unknown-size RIFF/data fields (`0xFFFFFFFF`); that is intentional. `test_out.wav` is the actual downloaded response with these two length fields finalized locally after completion, leaving its PCM audio unchanged. It is 48 kHz, mono, PCM16. No playback device or speaker settings are changed.

Re-run endpoint/WAV/SSE/CORS/socket checks (this overwrites the local verification WAV/evidence, not source):

```bat
cd /d C:\AI-Workspace\Projects\vieneu-studio
VieNeu-TTS\.venv\Scripts\python.exe verify-local.py
```

Upstream 8-client benchmark, keeping CUDA dependencies intact:

```bat
cd /d C:\AI-Workspace\Projects\vieneu-studio\VieNeu-TTS
set "UV_NO_SYNC=1"
set "UV_PYTHON_DOWNLOADS=never"
set "PYTHONUTF8=1"
"%USERPROFILE%\.local\bin\uv.exe" run python examples/openai_speech_client.py --bench 8
```

With four active slots, eight simultaneous clients include queued requests. Their end-to-end TTFA/RTF must not be compared directly with upstream's 8- or 16-slot benchmark numbers. TTFA measures arrival of actual PCM, not the WAV header. RTF is wall time divided by audio duration; lower is better and <1 is faster than real-time. GPU idling, power limits, other workloads and laptop hardware affect these numbers. No clocks or power-management settings were changed.

## Voices, models and storage

- Built-in presets: `VieNeu-TTS\src\vieneu\assets\voices_v3_turbo.json` (25 presets bundled in the inspected source). No preset file was edited.
- `POST /v1/voices` accepts multipart fields **`name`**, **`file`**, optional **`denoise`** (default true), optional **`description`** (default empty). It was **not called** during testing.
- Enrolled custom voices live **only in this server process's RAM** and disappear at restart. The uploaded temporary clip and SDK pre-cleaned clip are removed by upstream. No permanent custom-voice directory/database exists in this installation.
- Do not call the SDK's parameterless `save_voices()` here: it writes the bundled source preset JSON. Persistence is not implemented by this HTTP server; future work must use an explicit file outside the clone and implement loading.
- Model downloads use the launcher's process-local `HF_HOME`: `vieneu-studio\cache\huggingface`. No global cache setting was changed. The existing per-user uv package cache remains managed by uv.
- Python virtual environment: `VieNeu-TTS\.venv`; editable install metadata and Python bytecode inside the clone are generated dependency/runtime artifacts, not edits to upstream source.
- Source-level API contract and all endpoints: [`docs/API.md`](docs/API.md). CORS is **not enabled**. A future extension must handle its browser permissions/request context; no CORS workaround was installed now.

## Update upstream safely

Stop the server first. Review upstream changes before updating. Commands below are manual instructions; no update beyond the initial clone was run.

```bat
cd /d C:\AI-Workspace\Projects\vieneu-studio\VieNeu-TTS
git status --short
git pull --ff-only
set "UV_PYTHON_DOWNLOADS=never"
"%USERPROFILE%\.local\bin\uv.exe" sync --extra cuda --locked
```

Review `.python-version`, README installation/streaming changes and the lockfile before syncing. If a new interpreter or another prerequisite is needed, ask the owner; do not silently install it. `--locked` prevents a local lockfile rewrite. Do not use a bare `uv sync` for this GPU installation: it selects the core CPU dependency set. If CUDA installation fails, CPU is an explicit fallback that must be reported; it is not the configured launcher here. After an update, repeat verification and check the listening socket. There are no commits/pushes in this setup.

## Evidence and limits

- `logs/install-cuda.log`: actual installation output.
- `logs/server.log`: actual startup and request output for the managed verification instance.
- `logs/health.json`, `models.json`, `voices.json`, `openapi.json`, `verification.json`: actual HTTP responses and verification details.
- `logs/bench8.log`: actual unmodified upstream client's 8-concurrent-client result.
- `logs/vram.csv`, `logs/vram-summary.json`: sampled total-device `nvidia-smi` usage, including Windows and other applications, not a process-only VRAM claim.
- `record-vram.py` is a bounded read-only sampler used for the installation test; it does not change GPU settings. It refuses to overwrite its prior CSV evidence.

See `docs/INSTALLATION.md` for the final installed versions, measured benchmark/VRAM values and any warnings. A binary/non-silent WAV check does not substitute for a human listening-quality review.
