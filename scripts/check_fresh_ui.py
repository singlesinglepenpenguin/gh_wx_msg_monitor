"""Check the real first-run UI after an explicit reset; never connect to WeChat."""
from pathlib import Path
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from group_msg_monitor.app import App
from group_msg_monitor.paths import data_home


def main():
    local = data_home()
    if local.exists() and any(local.iterdir()):
        raise SystemExit('Reset runtime data before checking the fresh UI.')
    app = App()
    errors = []

    def check():
        try:
            assert not app.connection_ok
            assert not app.auto.get()
            assert app.vars['roster'].get() == ''
            assert app.vars['target'].get() == ''
            assert app.last_result is None
            app._connection()
            app.after(600, check_wizard)
        except Exception as exc:
            errors.append(repr(exc))
            app.destroy()

    def check_wizard():
        try:
            wizard = app.settings_window
            assert wizard.entries == {}
            assert wizard.selected.get() == ''
            assert not wizard.connecting and not wizard.waiting
            assert not (local / 'wechat.json').exists()
            assert not (local / 'decrypted').exists()
            assert not local.exists() or not any(local.iterdir())
            print(json.dumps({'status': 'passed', 'check': 'real fresh source GUI only',
                              'account_picker': 'empty', 'files': 'empty', 'auto': False,
                              'credentials_and_decrypted_cache': 'absent',
                              'wechat_initialization_or_read': 'not performed'}))
        except Exception as exc:
            errors.append(repr(exc))
        finally:
            app.destroy()  # No preference write during a read-only acceptance check.

    app.after(700, check)
    app.mainloop()
    if errors:
        raise SystemExit('; '.join(errors))


if __name__ == '__main__':
    main()
