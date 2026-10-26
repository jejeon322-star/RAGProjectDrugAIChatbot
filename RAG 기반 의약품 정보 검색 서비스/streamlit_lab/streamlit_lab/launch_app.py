"""중복 실행을 피하고 시작 상태를 확인한 뒤 브라우저를 연다."""
from pathlib import Path
import os
import subprocess
import sys
import time
from urllib.request import urlopen
import webbrowser

ROOT = Path(__file__).resolve().parent
URL = 'http://localhost:8502'


def healthy():
    try:
        with urlopen(URL + '/_stcore/health', timeout=2) as response:
            return response.status == 200 and response.read().strip() == b'ok'
    except Exception:
        return False


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    no_browser = '--no-browser' in sys.argv
    if healthy():
        if not no_browser:
            webbrowser.open(URL)
        print('챗봇이 이미 실행 중입니다: ' + URL)
        return 0
    python = ROOT / '.venv_drug/Scripts/python.exe'
    if not python.exists():
        print('실행 환경이 없습니다. .venv_drug 폴더를 확인하세요.')
        return 1
    logs = ROOT / 'data/hospital/project/storage/logs'
    logs.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    environment['PYTHONUTF8'] = '1'
    environment['DRUG_PROJECT_ROOT'] = str(ROOT / 'data/hospital/project')
    with (logs / 'server.log').open('ab') as output:
        process = subprocess.Popen([str(python), '-m', 'streamlit', 'run', str(ROOT / 'hospital_app.py'),
                    '--server.port', '8502', '--server.address', '127.0.0.1', '--server.headless', 'true'],
                    cwd=str(ROOT), stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                    env=environment, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    for _ in range(60):
        if healthy():
            if not no_browser:
                webbrowser.open(URL)
            print('챗봇을 열었습니다: ' + URL)
            return 0
        if process.poll() is not None:
            break
        time.sleep(.5)
    print('시작하지 못했습니다. 서버 기록: ' + str(logs / 'server.log'))
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
