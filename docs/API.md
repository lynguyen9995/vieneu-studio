# VieNeu-TTS v3 Turbo: local HTTP API

Source inspected: upstream commit `04044e1b8f822f946bafb2c88b716459f4164152`, `apps/openai_speech.py` (all 465 lines), plus the voice implementation in `src/vieneu/v3turbo.py`. This file documents the code, not just README claims. No upstream source was changed. Clone enrollment is documented only; no voice was cloned during installation.

## Step 2 measured clarification (2026-10-31)

Actual HTTP outputs were checked before writing the gateway: binary WAV, binary PCM, SSE PCM and SSE WAV all default to **48,000 Hz**. All four also returned **24,000 Hz when explicitly requested with `sample_rate: 24000`**. The step-1 SSE test intentionally requested 24 kHz; SSE does not impose that rate. WAV was inspected at its RIFF sample-rate field; SSE was base64-decoded and its done usage/sample count checked; raw PCM has no self-describing rate, so `X-Sample-Rate` is the required interpretation contract. Byte counts matched the declared rates/durations. Detailed actual measurements: `logs/step2-measurements.json`.

With the existing four-stream GPU server, unmodified upstream client results were:

| Clients | TTFA median / max | Worst RTF | Success |
|---|---|---|---|
| 1 | 312 / 312 ms | 0.65 | 1/1 |
| 4 | 235 / 237 ms | 0.64 | 4/4 |

Runs are individual observations, affected by GPU idle clocks; not a statistical latency guarantee. Earlier bench8 is a queueing/overload result, not single-user latency. Used `UV_NO_SYNC=1`, `UV_PYTHON_DOWNLOADS=never`, `PYTHONDONTWRITEBYTECODE=1`; no upstream dependency changes.

Exact AST-extracted upstream upload handler was executed with a fake model and dummy nonvoice bytes: second upload to a custom name overwrites; protected built-in name returns 409. No real enrollment occurred. Live OpenAPI exposes no DELETE route; `DELETE /v1/voices/Step2-Nonexistent` returned 404. The gateway cannot evict upstream RAM through HTTP: deleting locally must prevent reuse through the gateway and stop replaying the deleted voice, while upstream restart clears the residual RAM entry.

## Local deployment and access

- Base URL: `http://127.0.0.1:8000` (`/v1` for OpenAI-compatible clients).
- `HOST=127.0.0.1`, `PORT=8000`, one Uvicorn worker. Source defaults to loopback; `docs/streaming.md` contains a contradictory introductory sentence saying all variants listen on `0.0.0.0`. The environment table and current source default to `127.0.0.1`.
- `VIENEU_MAX_STREAMS=4` limits concurrent generations. Queue capacity defaults to the same value (4); timeout defaults to 10 seconds. Eight benchmark clients therefore exercise both serving and queuing.
- Optional `VIENEU_API_KEY`: when set, the four `/v1` routes require `Authorization: Bearer <key>`. It is read from the process environment; no secret is stored here. `/health` and the automatic documentation endpoints are not protected by that dependency.
- No CORS middleware is registered. No `Access-Control-Allow-Origin`, allow-headers or exposed-headers policy is configured. A future Chrome extension must address extension host permissions and the context making the request; do not assume a cross-origin page/content-script fetch will pass CORS. Enabling CORS is outside this installation's scope.
- No TLS, rate limiter, permanent voice database, remote tunnel, Windows auto-start or reverse proxy is configured.

## Endpoint inventory

| Method | Path | Request | Success response |
|---|---|---|---|
| GET | `/health` | none | JSON health and load, 200 (or 503 when scheduler is down) |
| GET | `/v1/models` | none; optional configured Bearer auth | JSON model list, 200 |
| GET | `/v1/voices` | none; optional configured Bearer auth | JSON voice list, 200 |
| POST | `/v1/audio/speech` | JSON; optional configured Bearer auth | Chunked audio bytes or SSE, 200 |
| POST | `/v1/voices` | multipart form; optional configured Bearer auth | JSON enrolled voice summary, 200 |
| GET | `/openapi.json` | none | FastAPI-generated OpenAPI JSON |
| GET | `/docs` | none | FastAPI Swagger UI HTML (automatic upstream route) |
| GET | `/docs/oauth2-redirect` | none | Swagger OAuth redirect HTML (automatic upstream route) |
| GET | `/redoc` | none | FastAPI ReDoc HTML (automatic upstream route) |

There is no application root `/`, voice-delete endpoint, voice-save endpoint, file-download endpoint, WebSocket, `/v1/audio/transcriptions`, or `/v1/audio/voices` route. HEAD/OPTIONS handlers are not explicitly implemented by the application. The generated documentation HTML may need external CDN resources; it is not a custom UI built for this installation.

## GET /health

No request body or query fields. No auth required, even when `VIENEU_API_KEY` is set.

Healthy JSON:

```json
{"status":"ok","backend":"pytorch","max_streams":4,"active":0,"waiting":0,"sample_rate":48000}
```

`backend` can also be `onnx`. `active` counts acquired generation slots; `waiting` counts requests waiting for one. If the GPU scheduler dies, HTTP 503 has the same fields with `status: "error"` plus `error: "stream scheduler is down: ..."`. Restart the process to recover; the scheduler does not self-restart. Startup loads and warms the model before Uvicorn starts accepting normal traffic.

## GET /v1/models

No request body or query fields. JSON shape:

```json
{
  "object":"list",
  "data":[{
    "id":"vieneu-v3-turbo","object":"model","created":0,"owned_by":"vieneu",
    "sample_rate":48000,"backend":"pytorch","max_streams":4,
    "response_formats":["pcm","wav"],"stream_formats":["audio","sse"],
    "sample_rates":[48000,24000,16000,8000]
  }]
}
```

`created: 0` is a compatibility constant, not an actual model creation timestamp.

## GET /v1/voices

No request body or query fields. Returns both built-in presets and voices enrolled in this process:

```json
{
  "object":"list",
  "data":[{
    "id":"Mai Anh","name":"Mai Anh","description":"...","gender":"...",
    "featured":4,"aliases":[]
  }]
}
```

This is an illustrative entry, not a recorded response. `id` and `name` are the canonical voice label. `description`/`gender` default to empty strings, `featured` can be null, and `aliases` is a list. Use the returned names or aliases verbatim for synthesis. The server preserves the preset dictionary's insertion order; it does not apply the SDK's separate featured-first sort in this endpoint. The bundled default voice is Hải Đăng.

## POST /v1/audio/speech

Content-Type: `application/json`. Body fields:

| Field | Type | Default / required | Constraints and behavior |
|---|---|---|---|
| `input` | string | **required** | 1–20,000 characters; split internally at sentence boundaries using `max_chars` |
| `model` | string | `vieneu-v3-turbo` | Compatibility field; any string accepted, does not select another model. Audio response does not echo it. |
| `voice` | string or null | null | Preset/enrolled name or alias; null/empty uses the engine's default voice. Unknown nonempty name → 400. |
| `response_format` | string | `wav` | `pcm` or `wav`, case-insensitive. MP3/Opus/AAC/FLAC output unsupported → 400. |
| `stream_format` | string | `audio` | Exactly `audio` or `sse` (case-sensitive) |
| `sample_rate` | integer | 48000 | Exactly 48000, 24000, 16000 or 8000; stateful soxr resampling from 48 kHz |
| `speed` | number or null | null | Accepted but ignored; non-null reported in `X-VieNeu-Ignored` |
| `instructions` | string or null | null | Accepted but ignored; non-null reported in `X-VieNeu-Ignored` |
| `temperature` | number | 0.8 | Finite, 0–2 inclusive |
| `top_k` | integer | 25 | 1–1024 inclusive |
| `top_p` | number | 0.95 | Finite, greater than 0 and at most 1 |
| `repetition_penalty` | number | 1.2 | Finite, 1–2 inclusive |
| `max_chars` | integer | 256 | 64–512 inclusive |

The Pydantic model uses its default extra-field policy (unknown fields ignored, not additional supported knobs). No request field for an arbitrary reference path, upload, `denoise`, persistent save, output filename or non-streaming mode is provided. Choose voices by name after enrollment. `style` is not an API knob; the SDK's v3 Turbo style option is deprecated/ignored.

Example body:

```json
{"model":"vieneu-v3-turbo","input":"Xin chào, đây là bản kiểm tra giọng đọc tiếng Việt.","voice":"Mai Anh","response_format":"wav"}
```

### Binary streaming (`stream_format: "audio"`)

Audio streams by default; a separate `stream: true` field is unnecessary.

- `response_format: "pcm"`: `Content-Type: audio/pcm`, raw signed 16-bit **little-endian**, mono, requested sample rate, no header.
- `response_format: "wav"`: `Content-Type: audio/wav`, 44-byte RIFF/WAVE PCM header followed by the same PCM bytes. RIFF/data sizes are `0xFFFFFFFF` (unknown length), so playback can begin immediately. A finite downloaded WAV can have these size fields finalized locally after the response ends.
- HTTP/1.1 response is chunked; there is no predetermined Content-Length. Header chunks are not audio and must not count as TTFA.
- Common response headers: `X-Request-Id: spk-<8 hex chars>`, `X-Sample-Rate`, `Cache-Control: no-store`. `X-VieNeu-Ignored: speed,instructions` appears only when those fields are non-null.
- Disconnecting closes generation and releases the slot. A runtime failure after 200 headers have been sent may truncate the body rather than return a new JSON error.

### SSE streaming (`stream_format: "sse"`)

`Content-Type: text/event-stream; charset=utf-8`. Each event is `data: <JSON>\n\n` (not a named `event:` field):

```text
data: {"type":"speech.audio.delta","audio":"<base64 PCM16LE bytes>"}

data: {"type":"speech.audio.done","usage":{"output_samples":222720,"sample_rate":48000,"seconds":4.64}}

```

`output_samples` counts mono samples at the requested output rate. `seconds` is rounded to three decimals. There is no `[DONE]` marker. With `response_format: "wav"`, the first delta contains the base64 WAV header; later deltas contain PCM. With `pcm`, every delta contains PCM. SSE does not encode MP3/Opus.

## POST /v1/voices — clone-upload contract (NOT exercised)

**Content-Type: `multipart/form-data` with a boundary.** This is not a JSON request and not a base64-audio endpoint. Exact form part names:

| Part | Form type | Required/default | Meaning |
|---|---|---|---|
| `name` | text/string | **required** | New voice name, 1–64 characters, exact validation below |
| `file` | binary file upload | **required** | Recommended clean 3–8 second reference clip; filename extension chooses temporary suffix |
| `denoise` | boolean form text | optional, `true` | Use `true` or `false` for interoperability; defaults to reference denoising |
| `description` | text/string | optional, empty | Metadata; uses the same restrictive name pattern when nonempty |

Conceptual request (not sent):

```text
POST /v1/voices
Content-Type: multipart/form-data; boundary=<generated-by-client>

name=<consented speaker label>
file=@<reference.wav>
denoise=true
description=<optional label>
```

Name validation is Python `re.fullmatch(r"[^\W_][\w .\-]{0,63}", value)`: first character is a Unicode letter/digit (not underscore); remaining characters may include Unicode word characters, spaces, period, hyphen or underscore. No leading space/punctuation, slashes, colon, newline or arbitrary prose punctuation. `description` may be empty; otherwise the exact same 1–64-character pattern is enforced. Thus a long description or one containing a comma is rejected.

### Accepted formats and limits

- Recognized filename extensions (case-insensitive): `.wav`, `.mp3`, `.flac`, `.ogg`, `.m4a`.
- An unknown/missing extension is **not rejected at the route boundary**; it maps to a `.wav` temporary suffix. The code does not validate the uploaded MIME type or perform an extension whitelist rejection.
- The reference pre-cleaner uses `soundfile.read`/libsndfile before the downstream model. Therefore these five recognized suffixes are **not a guarantee that all encodings decode**. PCM WAV is the safest choice; FLAC/OGG/MP3 depend on the installed libsndfile codec support; M4A/AAC is generally not supported by libsndfile. No FFmpeg was added for this installation. Actual decode/enrollment failure → 400. Supported upload decoders should be distinguished from the two supported **output** formats.
- Maximum file bytes read: `20 * 1024 * 1024` (20 MiB; error wording says 20 MB). Larger → 413. This read-time check happens after multipart parsing; it is not a reverse-proxy/body-size limit.
- 3–8 seconds is the documented recommendation, not a route-level duration validator. The engine's reference preparation cleans/trims the clip; a caller cannot control duration through this route.

### Response, collisions and storage

Success is HTTP **200**, not 201:

```json
{"id":"New Voice","name":"New Voice","description":"Optional label"}
```

- Reusing a built-in voice name **or alias captured at engine startup** → 409. Existing custom names are not in that protected set and can be overwritten by another upload using the same name.
- The upload is written to a uniquely named temporary file using `tempfile.mkstemp`. It is removed in a `finally` block, whether enrollment succeeds or fails. SDK pre-cleaning also uses/removes an intermediate WAV under `~/.cache/vieneu/tmp`.
- The engine keeps a float32 **192-dimensional speaker embedding**, int64 reference audio codes, and metadata in process memory (`tts._preset_voices[name]`). It does not keep the original uploaded audio as a permanent voice file.
- The route calls `tts.add_voice(..., denoise=..., description=...)` **without `save=True`** and never calls `save_voices()`. Newly cloned voices are **lost on restart**. There is no HTTP endpoint or environment switch in this server for saving/reloading custom voices.
- Built-in presets are bundled at `VieNeu-TTS/src/vieneu/assets/voices_v3_turbo.json` in this editable clone. Additional model-shipped presets may be loaded from the model repository.
- The SDK separately exposes `save_voices(path=...)`; its no-path default writes to that bundled source JSON. **Do not call that default here**, because upstream must stay unmodified. A future persistence feature must explicitly use a file beside the clone and implement loading; none was implemented now.
- `gender`, `save`, `ref_text`, `use_ref_codes`, `style`, `speaker_emb` and `codes` are **not form parameters** exposed by this HTTP route.

## Errors

For application HTTP exceptions and body/form validation handled in this module:

```json
{"error":{"message":"...","type":"invalid_request_error","code":400}}
```

`code` is normally the numeric HTTP status (despite OpenAI clients sometimes expecting a string). Validation errors are changed from FastAPI's usual 422 to **400**, reporting the first field/reason, without echoing submitted values.

| Status | Typical trigger |
|---|---|
| 400 | Invalid JSON/body/field, unsupported speech format/rate, unknown voice, invalid upload name/description, decode/enrollment failure |
| 401 | Missing/incorrect Bearer value when `VIENEU_API_KEY` is set |
| 409 | Upload tries to replace a built-in voice or startup alias |
| 413 | Reference upload exceeds 20 MiB |
| 429 | Admission queue full (`Retry-After: 1`) or timeout waiting for a slot (`Retry-After: 2`) |
| 503 | Dead GPU scheduler (`/health` has its own health JSON shape; speech has OpenAI-style error) |

Framework-generated missing routes/methods can use FastAPI/Starlette's normal `detail` shape, and unhandled internal exceptions may return 500. Do not assume every possible response shares the custom error envelope.

## Environment reference

| Variable | Upstream default | Meaning |
|---|---|---|
| `HOST` / `PORT` | `127.0.0.1` / `8000` | Listen address/port; launcher overrides explicitly to loopback |
| `VIENEU_BACKEND` | `auto` | `auto`, `pytorch` or `onnx` |
| `VIENEU_DEVICE` | `auto` | `auto`, `cuda` or `cpu` |
| `VIENEU_PRECISION` | `fp32` | CPU ONNX precision (`fp32`/`int8`); does not select GPU dtype |
| `VIENEU_ONNX_DIR` | unset | Optional local ONNX directory |
| `VIENEU_MAX_STREAMS` | GPU 16; CPU fp32 1/int8 2 | Launcher uses 4 for this GPU |
| `VIENEU_QUEUE` | max streams | Waiting queue capacity |
| `VIENEU_QUEUE_TIMEOUT` | 10 seconds | Max admission wait |
| `VIENEU_API_KEY` | unset | Optional Bearer auth; not stored in project files |
| `VIENEU_WATERMARK` | `1` | Requests watermark application; the optional watermark package is separate from the CUDA extra |

Watermark capability must not be inferred from `VIENEU_WATERMARK=1` alone: core/CUDA installation does not install `resemble-perth`. See installation verification for the actual package state.
