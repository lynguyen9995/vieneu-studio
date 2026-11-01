# Step 2 verification — 2026-10-31

## Measured before adapter implementation

- All defaults: WAV, raw PCM, SSE PCM and SSE WAV **48000 Hz**; explicit sample_rate=24000 produced **24000 Hz** in each combination. Checked actual WAV RIFF headers, X-Sample-Rate and decoded SSE usage/audio, not just README. Step1 SSE 24 kHz was an explicit request, not a default.
- Upstream benchmark `uv run python examples/openai_speech_client.py --bench 1`: 1/1 OK, TTFA median/max **312/312 ms**, RTF max **0.65**, wall2.7s.
- `--bench 4`: 4/4 OK, TTFA median/max **235/237 ms**, RTF max **0.64**, wall2.7s. Earlier bench8 includes queueing and is not single-user latency.
- Existing custom upstream name overwrites; presets/aliases409. No upstream DELETE route (404). Confirmed by source/fake model route tests, not a real voice clone.
- ffmpeg already available; no global install in Step2.
- Evidence: `logs/step2-measurements.json`, `logs/step2-bench1.log`, `logs/step2-bench4.log`.

## Components

- Root uv environment separate from upstream, Python3.12.16 and official MCP SDK1.30.0.
- Single upstream GPU model on127.0.0.1:8000, unchanged; lightweight FastAPI gateway on127.0.0.1:8001; CLI HTTP client; four-tool official-SDK stdio MCP.
- Gateway token generated into root `.env`, never included here. Explicit ALLOWED_ORIGINS defaults empty, no wildcard. CORS preflight handled outside bearer enforcement; actual calls require auth except health.
- Consent required, converted reference WAV and metadata in voices/. Gateway-startup replay; not continuous re-enrollment if upstream alone restarts.
- DELETE local records and tombstone, no upstream deletion supported. Gateway refuses preset/alias deletion and use of residual deleted names. Upstream remains a same-machine unauthenticated bypass; do not expose8000.
- PCM/WAV/SSE audio streams through incrementally. CLI fixes unknown WAV length fields after download. Upstream already chunks sentences, so CLI does not duplicate chunking.
- `start-all.bat` foreground supervisor reuses healthy known services and stops only children it started. No boot auto-start or host MCP config edit.

## Executed final checks

From workspace root, with TMP/TEMP/TMPDIR set to Hermes authorized scratch:

```text
.venv/Scripts/python.exe -m pytest tests -q --tb=short
66 passed, 1 warning in 4.12s; exit0
```

Warning: installed Starlette TestClient/httpx deprecation; no optional dependency installed just to suppress it. Covers restart persistence/replay using mock upstream plus synthetic nonvoice PCM, consent, token denied/missing, CORS allow/deny, all three streaming variants, M4A conversion, state hardlinks/junctions, partial metadata failure and deletion recovery, CLI and typed MCP/real stdio protocol.

Actual `start-all.bat` startup succeeded. A subsequent launch reused8000 and8001 and exited without a duplicate. Gateway was restarted after storage fixes. Actual CLI command:

```text
uv run --no-sync python cli/speak.py "Xin chào" --voice "Mai Anh"
```

Output: `C:\AI-Workspace\Projects\vieneu-studio\outputs\20261031-133928-223cfdf3e5eb.wav`, **0.72seconds, 48000Hz, mono PCM16**. Header/info inspected; no claim of listening to playback.

`verify-step2.py` completed exit0: real MCP stdio handshake and exactly `clone_voice`, `delete_voice`, `list_voices`, `speak`; real MCP list_voices HTTP200; tracked upstream SHA256 and upstream .venv/pyvenv.cfg unchanged; upstream git status clean; netstat listeners127.0.0.1:8000 and127.0.0.1:8001 only. Evidence `logs/step2-manual-final.log`, `logs/step2-verification.json`, `logs/step2-tests-final.log`.

## Review

Initial independent review found linked/junction internal state writes, partial metadata publication, and interrupted-deletion orphan recovery bugs. Added reproducing tests and fixed with link/reparse rejection, unpredictable exclusive atomic JSON publication, pending tombstone recovery, artifact precheck and idempotent deletion. Independent re-review passed with no remaining security_concerns or logic_errors within the stated static-link boundary. Reviewer independently ran 14 store/gateway/streaming tests and scratch-only fault probes; no real enrollment or secret reads. Nonblocking suggestions: persist additional fault-injection cases and retain the known TestClient deprecation warning.

## Limits and deviations

- No real-person cloning or preset-output re-cloning performed. Real enrollment quality untested; use a voice-owner-provided clip and consent, CLI clone then restart gateway/upstream to check replay, then delete. See GATEWAY.md and MCP-SETUP.md.
- Upstream custom overwrite intentionally becomes409 in gateway; delete explicitly first.
- An aligned premature EOF of unknown-length WAV cannot be distinguished from completion without upstream expected length; client removes detected failed/invalid downloads.
- Upload's20MiB clip check currently follows framework multipart parsing/spooling; authenticated upload resource exhaustion is not fully hardened. Same-user hostile filesystem races are not sandboxed. Do not publish this local service.
- First launcher attempt hit Windows slow connection-refusal timeout. Added bounded TCP refusal diagnosis and regression test; corrected launcher started successfully. No firewall/system setting changes.
- Subagent's first test run used default pytest system temp before scratch steering; subsequent runs used authorized scratch. No upstream or other project changes detected.
- Claude/Codex application integration not run; Codex executable absent from PATH. Documented ready-to-paste snippets and exact registration command only. No global config was edited.

## Files

New source/config: `.python-version`, `.gitignore`, `.env.example`, root `pyproject.toml`, `uv.lock`, `start-all.bat`, `gateway/{__init__,app,config,launch,run,store}.py`, `cli/speak.py`, `mcp/server.py`, `verify-step2.py`.

New tests: `tests/test_{clients,config,gateway,launcher,mcp,store,streaming}.py`.

New docs: `docs/GATEWAY.md`, `docs/MCP-SETUP.md`, `docs/STEP2-VERIFICATION.md`. Updated `README.md` and `docs/API.md`. Step1 files kept.

Generated local artifacts: root `.venv/`, `.env` (secret, gitignored), `voices/.presets`, `outputs/20261031-133928-223cfdf3e5eb.wav`, isolated test artifacts, `logs/step2-*`. Test fixtures are generated nonvoice data in isolated temporary directories, not preset output. No commits/pushes or model duplication.
