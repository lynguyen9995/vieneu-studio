"""Engine (model) control: which model runs on 127.0.0.1:8000, and (re)starting it.

The engine runs in the upstream checkout's own environment through engine/vieneu_adapter.py.
Selection is persisted in <root>/engine.json so start-all, the gateway and a rebuild agree.
CLI:  python -m gateway.engine run|stop|status   (run = foreground, used by start-server.bat)
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
from pathlib import Path

ENGINE_DIR = 'VieNeu-TTS'
PORT = 8000
OPTIONS = {
    'turbo-gpu': {'label': 'v3 Turbo · GPU', 'mode': 'v3turbo', 'backend': 'pytorch', 'device': 'cuda',
                  'precision': 'fp32', 'streams': 4, 'note': 'Khuyên dùng. 48 kHz, nhanh nhất, chất lượng tốt nhất.'},
    'turbo-cpu': {'label': 'v3 Turbo · CPU', 'mode': 'v3turbo', 'backend': 'onnx', 'device': 'cpu',
                  'precision': 'fp32', 'streams': 1, 'note': 'Không cần GPU. Cùng chất lượng, chậm hơn nhiều.'},
    'turbo-int8': {'label': 'v3 Turbo · CPU int8', 'mode': 'v3turbo', 'backend': 'onnx', 'device': 'cpu',
                   'precision': 'int8', 'streams': 2, 'note': 'CPU nhanh hơn ~1.6×. Cần CPU có VNNI, CPU cũ có thể méo tiếng.'},
    'nano': {'label': 'v3 Nano · CPU (preview)', 'mode': 'v3nano', 'backend': 'onnx', 'device': 'cpu',
             'precision': 'fp32', 'streams': 2, 'note': 'Rất nhẹ cho máy yếu. Chất lượng kém hơn Turbo rõ rệt, 11 giọng.'},
    'lora': {'label': 'Custom LoRA · GPU', 'mode': 'v3turbo', 'backend': 'pytorch', 'device': 'cuda',
             'precision': 'fp32', 'streams': 4, 'needs_path': True,
             'note': 'Model v3 Turbo bạn tự fine-tune (thư mục merged từ finetune/).'},
}
DEFAULT = {'preset': 'turbo-gpu', 'lora_path': ''}


def root_dir() -> Path:
    return Path(__file__).resolve().parents[1]


def load_config(root: Path) -> dict:
    try:
        cfg = json.loads((root / 'engine.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return dict(DEFAULT)
    return validate(cfg, check_path=False)


def validate(cfg: dict, check_path: bool = True) -> dict:
    preset = cfg.get('preset')
    if preset not in OPTIONS:
        raise ValueError('Unknown model option')
    path = str(cfg.get('lora_path') or '').strip()
    if OPTIONS[preset].get('needs_path'):
        if not path:
            raise ValueError('Custom LoRA needs the folder of the merged model')
        if check_path and not Path(path).is_dir():
            raise ValueError('LoRA folder not found')
    return {'preset': preset, 'lora_path': path if OPTIONS[preset].get('needs_path') else ''}


def save_config(root: Path, cfg: dict) -> None:
    tmp = root / 'engine.json.tmp'
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(root / 'engine.json')


def uv_path() -> str:
    win = Path.home() / '.local' / 'bin' / 'uv.exe'
    return str(win) if win.exists() else (shutil.which('uv') or 'uv')


def command_and_env(root: Path, cfg: dict):
    opt = OPTIONS[cfg['preset']]
    env = os.environ.copy()
    env.update(HOST='127.0.0.1', PORT=str(PORT), VIENEU_BACKEND=opt['backend'], VIENEU_DEVICE=opt['device'],
               VIENEU_PRECISION=opt['precision'], VIENEU_MAX_STREAMS=str(opt['streams']),
               VIENEU_QUEUE='16', VIENEU_QUEUE_TIMEOUT='300', STUDIO_MODE=opt['mode'],
               STUDIO_BACKBONE_REPO=cfg.get('lora_path') or '', HF_HOME=str(root / 'cache' / 'huggingface'),
               PYTHONUTF8='1', UV_NO_SYNC='1', UV_PYTHON_DOWNLOADS='never')
    cmd = [uv_path(), 'run', '--no-sync', 'python', str(root / 'engine' / 'vieneu_adapter.py')]
    return cmd, env, root / ENGINE_DIR


def listener_pid(port: int = PORT) -> int | None:
    """PID listening on 127.0.0.1:<port>, without extra dependencies."""
    try:
        if os.name == 'nt':
            out = subprocess.run(['netstat', '-ano', '-p', 'TCP'], capture_output=True, text=True, timeout=15,
                                 creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)).stdout
            for line in out.splitlines():
                parts = line.split()
                # State column is localized ("LISTENING", "수신 대기", …) and may contain spaces:
                # identify listeners by the wildcard foreign address, take the PID from the last column.
                if (len(parts) >= 5 and parts[0].upper() == 'TCP' and parts[1].endswith(f':{port}')
                        and parts[2] in ('0.0.0.0:0', '[::]:0') and parts[-1].isdigit()):
                    return int(parts[-1])
        else:
            out = subprocess.run(['ss', '-ltnpH', f'sport = :{port}'], capture_output=True, text=True, timeout=15).stdout
            m = re.search(r'pid=(\d+)', out)
            return int(m.group(1)) if m else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return None


def is_ours(port: int) -> bool:
    """Only stop processes that identify as VieNeu Studio on /health (never an unrelated app on the port)."""
    import urllib.request
    try:
        req = urllib.request.Request(f'http://127.0.0.1:{port}/health', headers={'User-Agent': 'vieneu-stop'})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=5) as r:
            body = json.loads(r.read().decode('utf-8'))
    except Exception:
        return False
    if port == 8001:
        return body.get('service') == 'vieneu-gateway'
    return body.get('status') == 'ok' and body.get('backend') in ('pytorch', 'onnx') and 'max_streams' in body


OUR_MARKERS = ('vieneu_adapter.py', 'apps.openai_speech', 'gateway.run', 'gateway.engine')


def process_cmdline(pid: int) -> str:
    try:
        if os.name == 'nt':
            out = subprocess.run(['powershell', '-NoProfile', '-Command',
                                  f'(Get-CimInstance Win32_Process -Filter "ProcessId={int(pid)}").CommandLine'],
                                 capture_output=True, text=True, timeout=20,
                                 creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            return out.stdout.strip()
        return Path(f'/proc/{int(pid)}/cmdline').read_bytes().replace(b'\0', b' ').decode(errors='replace')
    except (OSError, subprocess.SubprocessError, ValueError):
        return ''


def is_our_process(pid: int) -> bool:
    """A VieNeu Studio process (engine or gateway), even if it is hung and no longer answers /health."""
    cmd = process_cmdline(pid)
    return any(m in cmd for m in OUR_MARKERS)


def stray_pids() -> list[int]:
    """Leftover VieNeu Studio processes (old launchers, start-server.bat consoles stuck at
    'Terminate batch job (Y/N)?', engines/gateways). Windows only; matched by command line."""
    if os.name != 'nt':
        return []
    ps = ("Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and $_.CommandLine -like '*vieneu-studio*' -and "
          "$_.CommandLine -match 'start-server\\.bat|gateway\\.launch|gateway\\.engine run|vieneu_adapter|gateway\\.run|apps\\.openai_speech' } "
          "| ForEach-Object { $_.ProcessId }")
    try:
        out = subprocess.run(['powershell', '-NoProfile', '-Command', ps], capture_output=True, text=True, timeout=30,
                             creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    mine = {os.getpid(), os.getppid()}
    return [int(x) for x in out.split() if x.strip().isdigit() and int(x) not in mine]


def wait_free(port: int, timeout: float) -> bool:
    """True once nothing listens on 127.0.0.1:<port> (polls up to `timeout` seconds)."""
    import time
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if not listener_pid(port):
            return True
        time.sleep(0.5)
    return not listener_pid(port)


def kill_pid(pid: int) -> None:
    if os.name == 'nt':
        subprocess.run(['taskkill', '/PID', str(pid), '/T', '/F'], capture_output=True, timeout=30,
                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    else:
        os.kill(pid, signal.SIGTERM)


def spawn(root: Path, cfg: dict) -> subprocess.Popen:
    """Start the engine detached (survives the gateway), logging to logs/engine.log."""
    cmd, env, cwd = command_and_env(root, cfg)
    (root / 'logs').mkdir(exist_ok=True)
    log = (root / 'logs' / 'engine.log').open('a', encoding='utf-8')
    flags = (getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0) | getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    return subprocess.Popen(cmd, cwd=cwd, env=env, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                            creationflags=flags, start_new_session=(os.name != 'nt'))


def log_tail(root: Path, lines: int = 12) -> str:
    try:
        data = (root / 'logs' / 'engine.log').read_text(encoding='utf-8', errors='replace').splitlines()
    except OSError:
        return ''
    return '\n'.join(data[-lines:])


def main(argv=None) -> int:
    argv = argv or sys.argv[1:]
    root = root_dir()
    cfg = load_config(root)
    action = argv[0] if argv else 'run'
    if action == 'status':
        print(json.dumps({'config': cfg, 'label': OPTIONS[cfg['preset']]['label'], 'pid': listener_pid()}, ensure_ascii=False))
        return 0
    if action == 'stop':
        for port in (8001, PORT):
            pid = listener_pid(port)
            if not pid:
                print(f'127.0.0.1:{port}: nothing running')
                continue
            if not is_ours(port):
                # Stopping the gateway makes its launcher shut the engine down too: give that a moment.
                if port == PORT and wait_free(port, 25):
                    print(f'Stopped 127.0.0.1:{port} (shut down by its launcher)')
                    continue
                pid = listener_pid(port) or pid
                if is_our_process(pid):
                    kill_pid(pid)
                    print(f'Stopped hung VieNeu process on 127.0.0.1:{port} (pid {pid})')
                    continue
                print(f'127.0.0.1:{port}: pid {pid} is NOT VieNeu Studio (or not answering) - left running. '
                      f'Stop it yourself only if you are sure it is ours.')
                continue
            kill_pid(pid)
            print(f'Stopped 127.0.0.1:{port} (pid {pid})')
        strays = stray_pids()
        for pid in strays:
            kill_pid(pid)
        if strays:
            print(f'Cleaned up {len(strays)} leftover VieNeu Studio process(es).')
        busy = [p for p in (8001, PORT) if not wait_free(p, 30)]
        print('Done: ports 8000 and 8001 are free. Safe to start again.' if not busy
              else f'Still busy: {busy}. Wait a few seconds before starting again.')
        return 0
    cmd, env, cwd = command_and_env(root, cfg)
    print(f"VieNeu engine: {OPTIONS[cfg['preset']]['label']} on http://127.0.0.1:{PORT} (Ctrl+C to stop)", flush=True)
    return subprocess.call(cmd, cwd=cwd, env=env)


if __name__ == '__main__':
    raise SystemExit(main())
