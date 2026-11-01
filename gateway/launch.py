"""Foreground supervisor: reuse healthy services, stop only its own children."""
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
import httpx

OPTIONAL = {8002}          # speech-to-text: nice to have, never blocks the voice engine and the gateway


def service_state(client, port, tcp_probe=None):
    try:
        r = client.get(f'http://127.0.0.1:{port}/health', timeout=2)
    except httpx.ConnectError:
        return 'missing'
    except httpx.ConnectTimeout as e:
        if tcp_probe is None:
            with socket.socket() as probe:
                probe.settimeout(5)
                result = probe.connect_ex(('127.0.0.1', port))
            if result in (10061, 111, 61):
                return 'missing'
        elif not tcp_probe(port):
            return 'missing'
        raise RuntimeError(f'Port {port} did not answer; not replacing an unknown process') from e
    except httpx.HTTPError as e:
        raise RuntimeError(f'Port {port} did not answer; not replacing an unknown process') from e
    try:
        body = r.json()
    except ValueError as e:
        raise RuntimeError(f'Port {port} belongs to an unrecognized service') from e
    if port == 8002:
        if r.status_code != 200 or body.get('status') != 'ok' or body.get('service') != 'vieneu-stt':
            raise RuntimeError('Port 8002 belongs to an unrecognized service')
        return 'ready'
    if r.status_code != 200 or body.get('status') != 'ok' or body.get('backend') not in ('pytorch', 'onnx'):
        raise RuntimeError(f'Port {port} belongs to an unrecognized/unhealthy service; inspect it manually')
    if port == 8001 and body.get('service') != 'vieneu-gateway':
        raise RuntimeError('Port 8001 belongs to an unrecognized service')
    return 'ready'


def state_with_retry(client, port, tries=20, pause=2):
    """A port that answers slowly is usually a model still starting or still shutting down
    (e.g. right after stop-all.bat). Wait for it instead of giving up at once."""
    for i in range(tries):
        try:
            return service_state(client, port)
        except RuntimeError as e:
            if 'did not answer' not in str(e):
                raise
            if i == tries - 1:
                from gateway import engine
                pid = engine.listener_pid(port)
                if pid and engine.is_our_process(pid):          # our own process, hung: replace it
                    print(f'Port {port}: VieNeu process {pid} is hung; stopping it.', flush=True)
                    engine.kill_pid(pid)
                    engine.wait_free(port, 30)
                    return 'missing'
                raise
            print(f'Port {port} is busy (starting or shutting down); waiting...', flush=True)
            time.sleep(pause)


def main():
    root = Path(__file__).resolve().parents[1]
    uv = Path.home() / '.local/bin/uv.exe'
    children = []
    ports = {}
    logs = []
    env = os.environ.copy()
    env.update(PYTHONUTF8='1', PYTHONDONTWRITEBYTECODE='1', UV_NO_SYNC='1', UV_PYTHON_DOWNLOADS='never')
    if hasattr(signal, 'SIGBREAK'):
        def interrupted(*_):
            raise KeyboardInterrupt
        signal.signal(signal.SIGBREAK, interrupted)
    print(f'VieNeu Studio launcher started (pid {os.getpid()}).', flush=True)
    try:
        with httpx.Client(trust_env=False) as client:
            for port, command, logfile in [
                (8000, ['cmd.exe', '/d', '/c', str(root / 'start-server.bat')], 'step2-upstream.log'),
                (8001, [str(uv), 'run', '--no-sync', 'python', '-m', 'gateway.run'], 'step2-gateway.log'),
                (8002, [str(uv), 'run', '--no-sync', 'python', '-m', 'stt.server'], 'stt.log'),
            ]:
                if port in OPTIONAL:
                    try:
                        if state_with_retry(client, port, tries=3) == 'ready':
                            print(f'Reusing existing healthy service on 127.0.0.1:{port}.', flush=True)
                            continue
                    except RuntimeError as e:
                        print(f'Skipping optional service {port}: {e}', flush=True)
                        continue
                elif state_with_retry(client, port) == 'ready':
                    print(f'Reusing existing healthy service on 127.0.0.1:{port}; it will NOT be stopped by this launcher.', flush=True)
                    continue
                log = (root / 'logs' / logfile).open('a', encoding='utf-8')
                logs.append(log)
                process = subprocess.Popen(command, cwd=root, env=env, stdout=log, stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,  # no hidden 'Terminate batch job (Y/N)?' prompt on Ctrl+Break
                    creationflags=getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0))
                children.append(process)
                ports[process.pid] = port
                deadline = time.monotonic() + (60 if port in OPTIONAL else 240)
                ready, problem = False, f'readiness timeout; inspect logs/{logfile}'
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        problem = f'exited; inspect logs/{logfile}'
                        break
                    try:
                        if service_state(client, port) == 'ready':
                            ready = True
                            break
                    except RuntimeError as e:
                        # We just started this process ourselves: a port that opens between the slow
                        # Windows connection-refused and the TCP probe is our service coming up, not a stranger.
                        if 'did not answer' not in str(e):
                            problem = str(e)
                            break
                    time.sleep(.5)
                if ready:
                    print(f'Ready: http://127.0.0.1:{port}', flush=True)
                elif port in OPTIONAL:
                    print(f'Optional service {port} not ready ({problem}). Narration still works without it.', flush=True)
                else:
                    raise RuntimeError(f'Service {port} {problem}')
        if not children:
            print('All services were already running. No new process started.', flush=True)
            return
        print('Services ready. Keep this window open; Ctrl+C stops only children started here.', flush=True)
        while True:
            for p in list(children):
                if p.poll() is None:
                    continue
                if ports.get(p.pid) in OPTIONAL:
                    print(f'Optional service {ports[p.pid]} ended; see logs/stt.log. Narration keeps working.', flush=True)
                    children.remove(p)
                    continue
                if ports.get(p.pid) == 8000:
                    # The studio's model switch stops this engine and starts a new one itself.
                    print('Engine process on 8000 ended (model switch from the studio, or a crash: see logs/engine.log). '
                          'Gateway keeps running.', flush=True)
                    children.remove(p)
                    continue
                raise RuntimeError('A managed service exited; inspect logs and restart start-all.bat')
            time.sleep(1)
    except KeyboardInterrupt:
        print('Stopping services started by this launcher...', flush=True)
    finally:
        for p in reversed(children):
            if p.poll() is None:
                try:
                    p.send_signal(signal.CTRL_BREAK_EVENT if os.name == 'nt' else signal.SIGINT)
                    p.wait(timeout=15)
                except (OSError, subprocess.TimeoutExpired):
                    if os.name == 'nt':
                        subprocess.run(['taskkill', '/PID', str(p.pid), '/T', '/F'], capture_output=True)
                    else:
                        p.kill()
        for f in logs:
            f.close()


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, OSError) as e:
        print(str(e), file=sys.stderr)
        raise SystemExit(1)
