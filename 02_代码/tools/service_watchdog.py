"""Windows login supervisor for the local Yanwei service (stdlib only)."""
import json
import logging
from logging.handlers import RotatingFileHandler
import msvcrt
import os
from pathlib import Path
import socket
import subprocess
import time
import urllib.request

CODE = Path(__file__).resolve().parents[1]
STATE = CODE.parent / '04_产物' / 'service'
HOST, PORT = '172.24.208.1', 8235
URL = f'http://{HOST}:{PORT}/api/health'


def healthy():
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(URL, timeout=3) as response:
            data = json.load(response)
        return data.get('ok') is True and data.get('service') == 'yanwei-demo'
    except (OSError, ValueError):
        return False


def address_available():
    try:
        with socket.socket() as probe:
            probe.bind((HOST, 0))
        return True
    except OSError:
        return False


def terminate_owned(child):
    # Only the process created by this supervisor and its descendants.
    if child is not None and child.poll() is None:
        subprocess.run(['taskkill', '/PID', str(child.pid), '/T', '/F'],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=subprocess.CREATE_NO_WINDOW, check=False)
        child.wait(timeout=15)


def main():
    STATE.mkdir(parents=True, exist_ok=True)
    lock = open(STATE / 'watchdog.lock', 'a+b')
    lock.seek(0)
    if not lock.read(1):
        lock.write(b'0')
        lock.flush()
    lock.seek(0)
    try:
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        return 0  # Another supervisor already owns recovery.
    log = logging.getLogger('yanwei.service')
    log.setLevel(logging.INFO)
    handler = RotatingFileHandler(STATE / 'watchdog.log', maxBytes=2_000_000,
                                  backupCount=3, encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    log.addHandler(handler)
    child, started, last_good, next_start, failures = None, 0, 0, 0, 0
    log.info('Supervisor started pid=%s endpoint=%s', os.getpid(), URL)
    try:
        while not (STATE / 'disabled').exists():
            now = time.monotonic()
            ok = healthy()
            if ok:
                last_good, failures = now, 0
            if child is not None and child.poll() is not None:
                log.warning('Service exited code=%s; retry scheduled', child.returncode)
                child = None
                failures += 1
                next_start = now + min(60, 5 * 2 ** min(failures - 1, 4))
            if child is not None and not ok and now - max(started, last_good) > 180:
                log.error('Owned service unresponsive for 180 seconds; restarting')
                terminate_owned(child)
                child = None
                next_start = now + 10
            if not ok and child is None and now >= next_start:
                if address_available():
                    env = dict(os.environ, PYTHONUTF8='1', HF_HUB_OFFLINE='1',
                               TRANSFORMERS_OFFLINE='1', PYTHONUNBUFFERED='1')
                    # Each attempt has a separate startup log; retain ten attempts.
                    logs = sorted(STATE.glob('service-*.log'), key=lambda p: p.stat().st_mtime)
                    for old in logs[:-9]:
                        old.unlink(missing_ok=True)
                    output_path = STATE / time.strftime('service-%Y%m%d-%H%M%S.log')
                    try:
                        with open(output_path, 'ab') as output:
                            child = subprocess.Popen(
                                [str(CODE / '.venv-multimodal/Scripts/python.exe'),
                                 str(CODE / 'run_demo.py'), '--host', HOST,
                                 '--port', str(PORT), '--no-browser'], cwd=CODE,
                                env=env, stdout=output, stderr=subprocess.STDOUT,
                                creationflags=subprocess.CREATE_NO_WINDOW)
                        started = now
                        log.info('Started service pid=%s log=%s', child.pid, output_path.name)
                    except OSError:
                        log.exception('Service launch failed')
                        next_start = now + 60
                else:
                    log.warning('Waiting for WSL host interface %s', HOST)
                    next_start = now + 30
            status = dict(supervisor_pid=os.getpid(), child_pid=child.pid if child else None,
                          healthy=ok, endpoint=URL, checked_at=time.strftime('%Y-%m-%d %H:%M:%S'))
            temp = STATE / 'status.tmp'
            temp.write_text(json.dumps(status, ensure_ascii=False), encoding='utf-8')
            temp.replace(STATE / 'status.json')
            time.sleep(5)
    finally:
        terminate_owned(child)
        log.info('Supervisor stopped')
        lock.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
