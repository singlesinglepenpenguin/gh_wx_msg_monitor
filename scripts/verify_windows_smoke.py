"""Windows smoke checks for bat entry, GUI, discovery, and Excel path (no live key capture)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / 'data/diagnostics/windows_smoke.json'
PYTHON = ROOT / '.venv/Scripts/python.exe'


def run(label, args, env=None, timeout=180):
    merged = os.environ.copy()
    if env:
        merged.update(env)
    result = subprocess.run(args, cwd=ROOT, env=merged, text=True, encoding='utf-8', errors='replace',
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
    (ROOT / 'data/diagnostics' / f'windows_{label}.log').write_text(result.stdout or '', encoding='utf-8')
    ok = result.returncode == 0
    print(f'{label}: {"PASS" if ok else "FAIL"}', flush=True)
    if not ok:
        raise RuntimeError(f'{label} failed; see windows_{label}.log')
    return result.stdout or ''


def main():
    report = {
        'checked_at': datetime.now(timezone.utc).isoformat(),
        'platform': sys.platform,
        'steps': [],
        'scope': 'Windows source bats + GUI + discovery + synthetic Excel; no debugger key capture',
    }
    try:
        if os.name != 'nt':
            raise RuntimeError('verify_windows_smoke.py is for Windows only')
        if not PYTHON.is_file():
            run('setup', ['cmd.exe', '/c', 'setup.bat', '--quiet'], timeout=300)
        env = {'PYTHONPATH': str(ROOT / 'src'), 'PYTHONDONTWRITEBYTECODE': '1'}
        out = run('runtime', [str(PYTHON), '-m', 'group_msg_monitor', '--check-runtime'], env)
        assert '"status": "ok"' in out or '"status":"ok"' in out
        report['steps'].append({'step': 'runtime', 'exit_code': 0})
        run('tests', [str(PYTHON), '-m', 'unittest', 'discover', '-s', 'tests'], env)
        report['steps'].append({'step': 'tests', 'exit_code': 0})
        run('layout', [str(PYTHON), str(ROOT / 'scripts/check_layout.py')], env)
        report['steps'].append({'step': 'layout', 'exit_code': 0})

        out = run('gui_excel_discovery', [str(PYTHON), str(ROOT / 'scripts/windows_probe.py')], env, timeout=90)
        report['probe'] = json.loads([line for line in out.splitlines() if line.startswith('{')][-1])
        report['steps'].append({'step': 'gui_excel_discovery', 'exit_code': 0, **report['probe']})

        # bat wrappers exist and setup is idempotent
        assert (ROOT / 'start.bat').is_file() and (ROOT / 'setup.bat').is_file()
        run('setup_idempotent', ['cmd.exe', '/c', 'setup.bat', '--quiet'], timeout=300)
        report['steps'].append({'step': 'setup_idempotent', 'exit_code': 0})
        report['status'] = 'passed'
    except BaseException as exc:
        report['status'] = 'failed'
        report['error'] = str(exc)
        raise
    finally:
        REPORT.parent.mkdir(parents=True, exist_ok=True)
        REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print(json.dumps({'status': report.get('status'), 'report': str(REPORT)}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
