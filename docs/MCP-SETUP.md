# VieNeu CLI and MCP setup (Windows)

These clients use only the authenticated HTTP gateway at `http://127.0.0.1:8001`.
They never import a model or start the upstream GPU server. Start `start-all.bat`
from `C:/AI-Workspace/Projects/vieneu-studio` before requesting audio.

The isolated root `.venv` must already be synced. Do not use or modify the upstream
`VieNeu-TTS` environment. `--no-sync` below prevents dependency synchronization.
`GATEWAY_TOKEN` and optional `GATEWAY_URL` are loaded programmatically from the root
`.env` regardless of the caller's working directory; existing environment variables
take precedence. Do not put the token in registration arguments or chat.

## CLI

Run from the project root (Git Bash examples):

```bash
.venv/Scripts/python.exe cli/speak.py 'Xin chào, đây là VieNeu.'
.venv/Scripts/python.exe cli/speak.py --file script.txt --voice 'Mai Anh'
.venv/Scripts/python.exe cli/speak.py 'Xin chào' -o 'C:/Audio/new-take.wav'
.venv/Scripts/python.exe cli/speak.py --list-voices
```

Synthesis prints metadata JSON followed by the absolute output path on the **last
stdout line**. Defaults are unique timestamped WAVs in `outputs/`. Existing files
are never overwritten. Explicit output must end in `.wav` and must not target
project source, environments, upstream, cache, or voice-storage directories.
Paths on another drive are allowed if explicit and safe. Streamed unknown WAV
lengths are finalized after download; failed downloads remove their partial file.
No audio bytes are returned through MCP.

UTF-8 scripts are accepted (with or without BOM). The client sends one request,
rejects blank text and text longer than 20,000 characters, and leaves sentence
chunking to upstream (`max_chars` defaults to 256). Sample rate and duration are
read from the returned WAV, not assumed to be 24 kHz.

### Voice ownership and deletion

The **voice owner must agree** before cloning. Do not enroll somebody else's
voice without permission. These are examples only; no real cloning was performed:

```bash
.venv/Scripts/python.exe cli/speak.py --clone 'My Voice' --ref 'C:/Audio/reference.wav' --consent --consent-note 'I own this voice and agree to cloning.'
.venv/Scripts/python.exe cli/speak.py --delete-voice 'My Voice'
```

CLI `--consent` is mandatory; if no note is supplied, it records an explicit
voice-owner agreement statement. MCP requires **both** `consent_confirmed: true`
and a nonblank `consent_note`; there is no MCP default consent. Clone upload is
multipart with `file`, `name`, `denoise=true`, description, and consent fields.
Deletion first checks the gateway's `kind` and refuses preset/unknown voices.
Gateway deletion masks the clone locally; unmodified upstream has no HTTP delete
and may retain it in RAM until restart.

## Official Python SDK MCP stdio server

Launch the script by absolute path, **not** `python -m mcp.server`. There is
intentionally no local `mcp/__init__.py`, so the installed official `mcp` SDK wins.
Stdout is reserved for MCP protocol messages; logs use stderr.

Portable launch command (Git Bash):

```bash
'C:/Users/Phuoc Ly/.local/bin/uv.exe' --directory 'C:/AI-Workspace/Projects/vieneu-studio' run --no-sync python 'C:/AI-Workspace/Projects/vieneu-studio/mcp/server.py'
```

It waits for a host's stdin requests; a quiet terminal is expected. The tools are:

| Tool | Arguments | Result |
|---|---|---|
| `speak` | `text`, `voice="Mai Anh"`, `out_path=null` | absolute `path`, `duration_seconds`, `sample_rate`, `voice` |
| `list_voices` | none | gateway object with `data` voice list and `kind` |
| `clone_voice` | `name`, `audio_path`, `consent_confirmed`, `consent_note` (all required) | gateway enrollment JSON |
| `delete_voice` | `name` | gateway deletion JSON; cloned voices only |

Expected failures are readable structured `{ "ok": false, "error": "..." }`
results. No tokens or audio bytes appear in tool results. Registration grants
local file creation and consented reference-file upload; retain host approval
for clone/delete actions and review output paths. The server itself is not a
filesystem sandbox; it runs with your Windows user permissions.

## Claude Desktop

**Example only: no global configuration was changed.** In Settings → Developer →
Edit Config, merge this into `%APPDATA%/Claude/claude_desktop_config.json`, keeping
other entries. Fully quit and restart Desktop afterward.

```json
{
  "mcpServers": {
    "vieneu": {
      "command": "C:/Users/Phuoc Ly/.local/bin/uv.exe",
      "args": [
        "--directory", "C:/AI-Workspace/Projects/vieneu-studio",
        "run", "--no-sync", "python",
        "C:/AI-Workspace/Projects/vieneu-studio/mcp/server.py"
      ]
    }
  }
}
```

Do not depend on an undocumented Desktop `cwd` field: `uv --directory` sets the
working directory portably. Inspect `%APPDATA%/Claude/logs` for startup errors.

## Claude Code

**Run only if you choose to register**; this command is documented, not executed.
From the project root, `--scope project` writes the project's `.mcp.json`, not a
user-global registration:

```bash
claude mcp add --transport stdio --scope project vieneu -- 'C:/Users/Phuoc Ly/.local/bin/uv.exe' --directory 'C:/AI-Workspace/Projects/vieneu-studio' run --no-sync python 'C:/AI-Workspace/Projects/vieneu-studio/mcp/server.py'
```

Use `/mcp` in Claude Code to inspect the connection and approve project servers.
The `--` separates host options from the server command. Avoid adding `.env` or a
token to `.mcp.json`.

## Codex

**Example only: no Codex configuration was changed.** Prefer project-scoped
`C:/AI-Workspace/Projects/vieneu-studio/.codex/config.toml` in a trusted project:

```toml
[mcp_servers.vieneu]
command = "C:/Users/Phuoc Ly/.local/bin/uv.exe"
args = ["--directory", "C:/AI-Workspace/Projects/vieneu-studio", "run", "--no-sync", "python", "C:/AI-Workspace/Projects/vieneu-studio/mcp/server.py"]
cwd = "C:/AI-Workspace/Projects/vieneu-studio"
startup_timeout_sec = 30
tool_timeout_sec = 600
```

Codex also supports `codex mcp add vieneu -- <command> <args...>`, but that
registration command writes the user Codex config by default; it was **not run**.
Use the project TOML above when global changes are unwanted. `codex mcp list` and
`/mcp` can inspect configured tools. Long scripts can exceed a host's default
60-second tool timeout; the example sets 600 seconds.

## Verification and troubleshooting

Focused tests mock HTTP synthesis/enrollment/deletion and exercise the official
SDK's typed registration plus a real stdio handshake from a different cwd.
They require no running gateway, real token, model load, or real cloning.

```bash
TMP='C:/Users/Phuoc Ly/AppData/Local/hermes/cache/scratch' TEMP='C:/Users/Phuoc Ly/AppData/Local/hermes/cache/scratch' TMPDIR='C:/Users/Phuoc Ly/AppData/Local/hermes/cache/scratch' .venv/Scripts/python.exe -m pytest tests/test_clients.py tests/test_mcp.py -q --basetemp tests/.tmp-clients -o asyncio_default_fixture_loop_scope=function
```

- Gateway offline or stream interrupted: `start start-all.bat`, then retry.
- HTTP 401/403: check the root `.env` token; don't paste it into logs or chat.
- No tools / import error: use the root env and the absolute script launch above;
  do not create `mcp/__init__.py` or use the upstream environment.
- Invalid/truncated WAV: incomplete output is removed; retry after checking
  gateway/upstream health. An aligned but prematurely ended unknown-length stream
  cannot be distinguished from a complete stream without a server-side length.
- No Codex binary on PATH: project TOML can be prepared by the user; the actual
  Codex/Claude application integration was not tested here.

Official references consulted for configuration syntax:

- https://code.claude.com/docs/en/mcp (stdio `add`, separator, project scope)
- https://developers.openai.com/codex/mcp (redirects to the current Codex MCP guide;
  project TOML, `cwd`, timeouts)
- https://raw.githubusercontent.com/openai/codex/main/codex-rs/cli/src/mcp_cmd.rs
  (Codex `add` writes user configuration; not executed)
- https://modelcontextprotocol.io/docs/develop/connect-local-servers (Desktop JSON)
