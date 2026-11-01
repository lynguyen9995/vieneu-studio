# Verified installation — 2026-10-31 (KST)

## Result

VieNeu-TTS v3 Turbo is installed and was actually served on the NVIDIA GPU. No CPU fallback was needed. The tested `start-server.bat` launched the current instance successfully. Last verified `/health`: HTTP 200, `status=ok`, `backend=pytorch`, `max_streams=4`, `active=0`, `waiting=0`, 48 kHz. The process was left running in the Hermes-managed terminal at completion; this is not a registered Windows service or boot auto-start.

Upstream source and lockfile remain unmodified: final `git status --porcelain=v1 --untracked-files=normal` was empty. Commit: `04044e1b8f822f946bafb2c88b716459f4164152`. No commits/pushes, voice enrollments, firewall grants, system changes, GPU clock changes, app/extension/MCP/UI development or edits to other project directories were performed.

## Prerequisites and installed versions

| Component | Verified value | Action |
|---|---|---|
| GPU | NVIDIA GeForce RTX 3060 Laptop GPU, 6144 MiB | Existing; `nvidia-smi` confirmed |
| NVIDIA driver | 591.59 | Existing; unchanged |
| Git | 2.53.0.windows.3 | Existing |
| uv | 0.13.6 (`329bf9448`, 2026-10-26) | Installed per user at `C:\Users\Phuoc Ly\.local\bin\uv.exe` |
| Python | 3.12.16 | Existing uv-managed interpreter; reused for repo `.python-version=3.12` |
| VieNeu package | 3.8.3 (editable local clone) | Installed into repo `.venv` |
| Model | `pnnbao-ump/VieNeu-TTS-v3-Turbo`, `update` subfolder | Downloaded on first start |
| Codec | `OpenMOSS-Team/MOSS-Audio-Tokenizer-Nano` | Downloaded on first start |
| torch | 2.8.0+cu128 | Installed, CUDA runtime 12.8 |
| torchaudio | 2.8.0+cu128 | Installed |
| transformers | 4.57.6 | Installed from upstream lock |
| onnxruntime | 1.24.4 | Installed core dependency, not the serving backend |
| FastAPI / Uvicorn | 0.120.2 / 0.38.0 | Installed dependencies |

CUDA availability and bf16 support both returned true. A real CUDA tensor allocation/sum passed. Python 3.14.9 also exists on the host/Hermes but was not used as the project interpreter. No new Python, driver or CUDA toolkit was installed. The missing `gh` executable was not needed for a public HTTPS git clone and was not installed.

Official uv Windows README command was run with process-local `UV_NO_MODIFY_PATH=1`:

```text
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
```

GPU environment installation (from clone): `uv sync --extra cuda --locked`, with `UV_PYTHON_DOWNLOADS=never`. It succeeded, preparing 82 packages in 11m18s and installing 89 in 5.23s. `--locked` is a protective addition preventing an upstream lockfile rewrite. No retry or CPU fallback was required.

## Exact start command

Run in Command Prompt:

```bat
C:\AI-Workspace\Projects\vieneu-studio\start-server.bat
```

It changes to `VieNeu-TTS`, sets loopback/port/max-streams/GPU/HF cache environment, and executes `"%USERPROFILE%\.local\bin\uv.exe" run python -m apps.openai_speech` with `UV_NO_SYNC=1`. This preserves installed CUDA extras without a fresh sync on each launch. Stop with Ctrl+C in its own console. Current managed server terminal: `proc_657010c6af4b`; Uvicorn Python PID observed during verification: 17112 (PIDs are transient).

## Verification results

- `GET /health`: 200, healthy PyTorch backend, four streams.
- `GET /v1/models`: 200, `vieneu-v3-turbo`, PCM/WAV, binary audio/SSE and sample rates 48000/24000/16000/8000.
- `GET /v1/voices`: 200, **25 built-in presets**; includes Mai Anh.
- `POST /v1/audio/speech`: 200, built-in **Mai Anh**, sentence: “Xin chào, đây là bản kiểm tra giọng đọc tiếng Việt trên máy tính của Ly.”
- Output: `C:\AI-Workspace\Projects\vieneu-studio\test_out.wav`, **4.08 seconds**, **48 kHz**, **mono PCM16**, **391,724 bytes**. RMS 0.10325, peak 0.64056; audio is non-silent. SHA-256: `890ec2e8c7a243194f609a3daf9bbd13a472315b1a07707933cf008b3439e5ee`.
- WAV was downloaded from the actual endpoint; only RIFF/data lengths were finalized from `0xFFFFFFFF` to finite file lengths for compatibility. PCM was not altered. Human listening quality has not been reviewed.
- Additional preset SSE request: 200, 24 kHz PCM, 8 events including done, 38,400 samples / 1.60 seconds / 76,800 PCM bytes; decoded byte count matched usage exactly.
- CORS test with extension Origin: no `Access-Control-Allow-Origin`; OPTIONS preflight returned **405**. Source contains no CORS middleware.
- `GET /openapi.json`: 200; schema saved. Static documentation audit covered all five explicit application endpoint definitions and all 13 `SpeechRequest` fields.
- Real listening socket: **`TCP 127.0.0.1:8000 ... LISTENING 17112`**. There was no listening `0.0.0.0:8000` or IPv6-wildcard port 8000 in the checked socket listing. The foreign-address `0.0.0.0:0` column is not a wildcard local bind.

### Requested upstream benchmark

Ran the unmodified client from the clone, preserving installed packages via process-local `UV_NO_SYNC=1`:

```text
uv run python examples/openai_speech_client.py --bench 8
```

Actual output:

```text
server: backend=pytorch max_streams=4 active=0
8 client (8 ok, 0 lỗi/429): TTFA median 1492 ms, max 2980 ms | RTF max 1.27 (cần < 1) | 5.2s tổng
```

**Interpretation:** all eight succeeded, but only four can generate simultaneously and the others queue. These client-side figures include queue time: worst RTF **1.27 > 1**, so eight simultaneous requests do not all meet end-to-end realtime at this configuration. Server-side generation logs (excluding admission wait) report RTF 0.57–0.63 for those eight requests, but that does not erase the measured client delay. Kept the user's requested four-stream ceiling; did not tune clocks, raise slots or hide this result.

Single WAV server log: TTFA 437 ms, 2.44 seconds generation / 4.08 seconds audio, RTF 0.60. This was a request after idle; it is not the eight-client median. Additional SSE server log: TTFA 147 ms, RTF 0.58.

### VRAM

Sampled `nvidia-smi --id=0 --query-gpu=index,memory.used,memory.total,utilization.gpu --format=csv,noheader,nounits` throughout startup and tests, 681 samples, 0.25-second nominal pause plus command overhead.

- Baseline before model start: **2117 MiB**.
- Overall observed peak (startup included): **3056 MiB** of 6144 MiB.
- Peak during requested bench8 phase: **3025 MiB**.
- Endpoint/WAV/SSE phase peak: **2697 MiB**.
- Overall peak minus baseline: **939 MiB**, an approximate device-level increase, **not a measured process-only allocation**.

These numbers include Windows, browsers and every other application on the GPU. They are sampled peaks, not a continuous maximum or `torch.cuda.max_memory_allocated()` value. Full evidence: `logs/vram.csv`, `logs/vram-summary.json`.

## Upload contract and persistence

`POST /v1/voices`: multipart **`name`** (required string), **`file`** (required binary), **`denoise`** (optional boolean, true), **`description`** (optional string, empty). Recognized filename suffixes: WAV, MP3, FLAC, OGG, M4A; actual decoder support is narrower. The installed libsndfile reports WAV/MP3/FLAC/OGG support, not M4A/AAC. No clone uploads were made to test codecs. Use WAV as the safest contract. Limit 20 MiB; recommended 3–8 seconds. Names and nonempty descriptions have the restrictive 1–64-character pattern documented in `API.md`. Built-in names/aliases return 409.

Enrollment is **RAM-only**, storing speaker embedding/reference codes/metadata; temporary audio is deleted by upstream and custom voices disappear on restart. There is no HTTP persistence/save endpoint. Built-ins: `VieNeu-TTS/src/vieneu/assets/voices_v3_turbo.json`. SDK's no-path `save_voices()` would modify that source JSON and must not be used here. No voice-cloning or persistence feature was built.

## Warnings and deviations

1. README benchmark hardware is desktop RTX 3060 **12 GB**, not this laptop 6 GB GPU. Do not promise its benchmark numbers for this machine.
2. `docs/streaming.md` introduction says `0.0.0.0`, but current source and its environment table default to **127.0.0.1**. Launcher pins loopback explicitly and the socket was checked.
3. Eight-client benchmark exceeds four active slots: high queued TTFA and worst client RTF 1.27; left visible rather than changing requested configuration.
4. Hugging Face symlink warning on Windows: downloads still worked but cache may use more disk. Developer Mode/admin privileges were **not** enabled.
5. Optional `hf_xet` absent: Hugging Face used regular HTTP successfully. Did not add the optional accelerator.
6. FastAPI `on_event` deprecation warning is emitted by upstream; startup still succeeded. Upstream was not patched.
7. `resemble-perth` is not installed by the requested CUDA extra. `VIENEU_WATERMARK=1` alone does not prove an actual watermark; no watermark package was added or watermark assertion made.
8. Upstream Transformers loads downloaded MOSS Python model code. Startup logged newly fetched `configuration_moss_audio_tokenizer.py` and `modeling_moss_audio_tokenizer.py`; this is upstream behavior, not code authored here. No security audit or separate revision pin was added. Model data uses the workspace HF cache; Hugging Face can also maintain its normal per-user dynamic-module cache.
9. GPU weights/model download made initial startup about **151.7 seconds**; cached restarts should differ. No startup-speed claim is inferred from upstream's cached test machine.
10. Process-local additions: local HF cache, explicit GPU backend/device, `UV_NO_SYNC=1` and `UV_PYTHON_DOWNLOADS=never`. No global environment/PATH settings changed. No firewall permissions were granted.

## Files created

Human-authored local files (all next to, not inside, upstream source):

- `start-server.bat`
- `README.md`
- `docs/API.md`
- `docs/INSTALLATION.md`
- `verify-local.py`
- `record-vram.py`

Generated verification files:

- `test-request.json`
- `test_out.wav`
- `logs/install-cuda.log`
- `logs/server.log`
- `logs/environment.json`
- `logs/health.json`
- `logs/models.json`
- `logs/voices.json`
- `logs/openapi.json`
- `logs/verification.json`
- `logs/verify-console.log`
- `logs/bench8.log`
- `logs/vram.csv`
- `logs/vram-summary.json`
- `logs/vram-phase.txt`
- `logs/vram-finish.txt`
- `logs/final-audit.json` and `logs/local-files.json` (machine-generated final audits)

Dependency-managed/generated trees (not individual authored source changes): the upstream clone and `.git`, repo `.venv` and build/bytecode/editable metadata, `cache/huggingface` models, existing per-user uv download/cache data and Hugging Face dynamic modules. The only new global/per-user executable installation is uv and its companion uvx/uvw launchers. No other project folder was modified.
