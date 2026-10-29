"""Test the staged source checkout without local data or installed dependencies.

Uses a temporary HOME and venv on this Mac, not a separate operating system.
Never initializes or queries real WeChat. Requires git and network for pip.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone


ROOT = Path(__file__).resolve().parents[1]
PROBE = '''
import json
from pathlib import Path
from group_msg_monitor.app import App
from group_msg_monitor.wechat_source import discover_accounts
app = App()
def verify():
    try:
        assert not app.busy, "startup check did not finish"
        assert not app.connection_ok, "unexpected inherited credentials"
        assert not app.vars['roster'].get() and not app.vars['target'].get()
        assert not (app.local_dir / 'wechat.json').exists()
        assert discover_accounts() == [], "unexpected account in empty HOME"
        app._connection()
        assert app.settings_window.winfo_exists()
        print(json.dumps({'first_launch': 'ok', 'settings': 'ok',
                          'credentials': 'absent', 'file_selection': 'empty',
                          'real_wechat_access': False}))
    except BaseException:
        import traceback
        traceback.print_exc()
        app.destroy()
        raise SystemExit(1)
    app.destroy()
app.after(2500, verify)
app.mainloop()
'''


def main():
    report_dir = ROOT / 'data/diagnostics'
    report_dir.mkdir(parents=True, exist_ok=True)
    report = {'checked_at': datetime.now(timezone.utc).isoformat(), 'steps': [],
              'scope': 'fresh source, HOME and venv on this OS; no real WeChat initialization'}
    env = {k: v for k, v in os.environ.items()
           if not k.startswith('PYTHON') and k not in ('VIRTUAL_ENV', 'GROUP_MSG_MONITOR_PYTHON')}
    with tempfile.TemporaryDirectory(prefix='group-monitor-clean-') as temporary:
        base = Path(temporary)
        source = base / 'workspace/source checkout'
        source.mkdir(parents=True)
        names = subprocess.check_output(['git', 'ls-files', '-z'], cwd=ROOT).decode().split('\0')
        for name in filter(None, names):
            if name.startswith('data/') and name not in (
                    'data/diagnostics/README.txt', 'data/diagnostics/error_message.md'):
                raise RuntimeError('Private runtime data must not be tracked: ' + name)
            target = source / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / name, target)
        home = base / 'empty home'
        home.mkdir()
        env.update(HOME=str(home), USERPROFILE=str(home), PYTHONDONTWRITEBYTECODE='1',
                   GROUP_MSG_MONITOR_PYTHON=sys._base_executable)
        try:
            def run(label, args, timeout=180):
                result = subprocess.run(args, cwd=source, env=env, text=True,
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
                (report_dir / ('clean_' + label + '.log')).write_text(result.stdout, encoding='utf-8')
                report['steps'].append({'step': label, 'exit_code': result.returncode})
                print(label + ': ' + ('PASS' if result.returncode == 0 else 'FAIL'), flush=True)
                if result.returncode:
                    raise RuntimeError(label + ' failed; see clean_' + label + '.log')
            if os.name == 'nt':
                run('setup', ['cmd.exe', '/c', 'setup.bat'], timeout=300)
                python = str(source / '.venv' / 'Scripts' / 'python.exe')
            else:
                run('setup', ['/bin/zsh', './setup.command'])
                python = str(source / '.venv/bin/python')
            env['PYTHONPATH'] = str(source / 'src')
            run('isolation', [python, '-c',
                'import sys; assert sys.prefix != sys.base_prefix; '
                'assert "include-system-site-packages = false" in '
                'open(sys.prefix + "/pyvenv.cfg").read(); print(sys.version)'])
            run('dependencies', [python, '-m', 'pip', 'check'])
            run('runtime', [python, '-m', 'group_msg_monitor', '--check-runtime'])
            run('tests', [python, '-m', 'unittest', 'discover', '-s', 'tests'])
            run('first_launch', [python, '-c', PROBE], timeout=30)
            report['status'] = 'passed'
        except BaseException as exc:
            report['status'] = 'failed'
            report['error'] = str(exc)
            raise
        finally:
            (report_dir / 'clean_environment.json').write_text(
                json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
