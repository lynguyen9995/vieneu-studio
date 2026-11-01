"""Verify only this loopback TTS service; never enroll a voice.

Run with VieNeu-TTS/.venv/Scripts/python.exe. Writes evidence beside clone.
The WAV response is saved, then its streaming length fields are finalized.
"""
from __future__ import annotations

import base64
import hashlib
import json
import pathlib
import struct
import subprocess
import time
import urllib.error
import urllib.request
import wave

ROOT = pathlib.Path(__file__).resolve().parent
BASE = "http://127.0.0.1:8000"
LOGS = ROOT / "logs"
LOGS.mkdir(exist_ok=True)
HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def request(path, *, body=None, method=None, headers=None):
    h = dict(headers or {})
    if body is not None:
        body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE + path, data=body, headers=h, method=method)
    return HTTP.open(req, timeout=120)


def main():
    evidence = {"base": BASE, "timestamp_local": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    for name, endpoint in (("health", "/health"), ("models", "/v1/models"), ("voices", "/v1/voices")):
        with request(endpoint) as r:
            data = json.load(r)
            assert r.status == 200
        (LOGS / f"{name}.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        evidence[name] = data
    assert evidence["health"]["status"] == "ok"
    assert evidence["health"]["backend"] == "pytorch", evidence["health"]
    assert evidence["health"]["max_streams"] == 4
    names = [v["id"] for v in evidence["voices"]["data"]]
    assert "Mai Anh" in names

    payload = {
        "model": "vieneu-v3-turbo", "voice": "Mai Anh", "response_format": "wav",
        "input": "Xin chào, đây là bản kiểm tra giọng đọc tiếng Việt trên máy tính của Ly."
    }
    (ROOT / "test-request.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    t0 = time.perf_counter()
    with request("/v1/audio/speech", body=payload) as r:
        audio = bytearray(r.read())
        evidence["speech_headers"] = dict(r.headers)
        assert r.status == 200
    evidence["wav_request_seconds"] = time.perf_counter() - t0
    assert audio[:4] == b"RIFF" and audio[8:12] == b"WAVE" and audio[36:40] == b"data"
    assert len(audio) > 44 and (len(audio) - 44) % 2 == 0
    evidence["raw_wav_size_fields"] = [struct.unpack_from("<I", audio, off)[0] for off in (4, 40)]
    struct.pack_into("<I", audio, 4, len(audio) - 8)
    struct.pack_into("<I", audio, 40, len(audio) - 44)
    out = ROOT / "test_out.wav"
    out.write_bytes(audio)
    with wave.open(str(out), "rb") as w:
        evidence["wav"] = {"path": str(out), "sample_rate": w.getframerate(), "channels": w.getnchannels(),
                           "sample_width": w.getsampwidth(), "frames": w.getnframes(),
                           "seconds": w.getnframes() / w.getframerate(), "bytes": len(audio),
                           "sha256": hashlib.sha256(audio).hexdigest()}
        pcm = w.readframes(w.getnframes())
    import numpy as np
    x = np.frombuffer(pcm, dtype="<i2").astype(np.float64) / 32768
    evidence["wav"]["peak"] = float(np.max(np.abs(x)))
    evidence["wav"]["rms"] = float(np.sqrt(np.mean(x * x)))
    assert evidence["wav"]["sample_rate"] == 48000
    assert evidence["wav"]["channels"] == 1 and evidence["wav"]["sample_width"] == 2
    assert evidence["wav"]["rms"] > 0.0001, "Silent WAV"

    # A second built-in-voice request validates actual SSE and resampling.
    sse_request = dict(payload, input="Chúc bạn một ngày tốt lành.", response_format="pcm",
                       stream_format="sse", sample_rate=24000)
    with request("/v1/audio/speech", body=sse_request) as r:
        raw_sse = r.read().decode("utf-8")
        evidence["sse_content_type"] = r.headers.get("Content-Type")
    events = [json.loads(line[6:]) for line in raw_sse.splitlines() if line.startswith("data: ")]
    assert events[-1]["type"] == "speech.audio.done"
    pcm_sse = b"".join(base64.b64decode(ev["audio"]) for ev in events if ev["type"] == "speech.audio.delta")
    assert len(pcm_sse) // 2 == events[-1]["usage"]["output_samples"]
    assert events[-1]["usage"]["sample_rate"] == 24000
    evidence["sse"] = {"event_count": len(events), "done": events[-1], "pcm_bytes": len(pcm_sse)}

    origin = "chrome-extension://vieneu-local-verification"
    with request("/health", headers={"Origin": origin}) as r:
        evidence["cors_get"] = {"status": r.status, "headers": dict(r.headers)}
    try:
        with request("/v1/audio/speech", method="OPTIONS", headers={
            "Origin": origin, "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type"
        }) as r:
            evidence["cors_preflight"] = {"status": r.status, "headers": dict(r.headers)}
    except urllib.error.HTTPError as e:
        evidence["cors_preflight"] = {"status": e.code, "headers": dict(e.headers), "body": e.read().decode()}
    assert not any(k.lower() == "access-control-allow-origin" for k in evidence["cors_get"]["headers"])

    with request("/openapi.json") as r:
        schema = json.load(r)
    (LOGS / "openapi.json").write_text(json.dumps(schema, ensure_ascii=False, indent=2), encoding="utf-8")
    evidence["openapi_paths"] = list(schema["paths"])
    evidence["netstat_8000"] = [line.strip() for line in subprocess.check_output(
        ["netstat", "-ano", "-p", "tcp"], text=True, encoding="utf-8", errors="replace").splitlines()
        if ":8000" in line and "LISTENING" in line]
    assert evidence["netstat_8000"], "No listening socket found"
    assert all("127.0.0.1:8000" in line for line in evidence["netstat_8000"]), evidence["netstat_8000"]
    with request("/health") as r:
        evidence["final_health"] = json.load(r)
    assert evidence["final_health"]["active"] == evidence["final_health"]["waiting"] == 0
    (LOGS / "verification.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = {"health": evidence["health"], "voice_count": len(names), "wav": evidence["wav"],
               "sse": evidence["sse"], "cors_preflight_status": evidence["cors_preflight"]["status"],
               "bind": evidence["netstat_8000"]}
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
