"""Bounded, read-only NVIDIA sampler for this installation's serving test.

No GPU settings are changed. Writes total-device usage, not process allocation
(on Windows WDDM the latter can be unavailable). Stop with logs/vram-finish.txt.
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import pathlib
import subprocess
import time

ROOT = pathlib.Path(__file__).resolve().parent
LOGS = ROOT / "logs"
STOP = LOGS / "vram-finish.txt"


def main():
    LOGS.mkdir(exist_ok=True)
    if STOP.exists():
        raise SystemExit("Finish marker already exists: refusing to overwrite prior test evidence")
    deadline = time.monotonic() + 1800
    count = 0
    baseline = peak = last = None
    phase_peaks = {}
    with (LOGS / "vram.csv").open("x", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["timestamp_local", "phase", "gpu_index", "memory_used_MiB", "memory_total_MiB", "gpu_util_percent"])
        while time.monotonic() < deadline and not STOP.exists():
            cp = subprocess.run([
                "nvidia-smi", "--id=0", "--query-gpu=index,memory.used,memory.total,utilization.gpu",
                "--format=csv,noheader,nounits"
            ], capture_output=True, text=True, timeout=10,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if cp.returncode:
                raise RuntimeError(cp.stderr)
            values = [int(v.strip()) for v in cp.stdout.strip().split(",")]
            phase_path = LOGS / "vram-phase.txt"
            phase = phase_path.read_text(encoding="utf-8").strip() if phase_path.exists() else "startup"
            stamp = dt.datetime.now().astimezone().isoformat(timespec="milliseconds")
            writer.writerow([stamp, phase, *values])
            f.flush()
            used = values[1]
            if baseline is None:
                baseline = used
            peak = used if peak is None else max(peak, used)
            last = used
            phase_peaks[phase] = max(phase_peaks.get(phase, 0), used)
            count += 1
            time.sleep(0.25)
    summary = {
        "measurement": "nvidia-smi total device memory.used; includes other applications and Windows",
        "nominal_pause_seconds": 0.25, "samples": count,
        "baseline_MiB": baseline, "peak_MiB": peak, "last_MiB": last,
        "peak_minus_baseline_MiB": peak - baseline if baseline is not None else None,
        "phase_peak_MiB": phase_peaks,
        "stop_reason": "verification_finished" if STOP.exists() else "1800_second_limit"
    }
    (LOGS / "vram-summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
