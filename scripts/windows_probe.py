"""One-shot Windows GUI/Excel/discovery probe used by verify_windows_smoke.py."""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'tests'))

from test_workflow import FUGUO, make_roster, make_target, message  # noqa: E402
from group_msg_monitor.app import App, open_path, result_summary  # noqa: E402
from group_msg_monitor.relay_core import update_file  # noqa: E402
from group_msg_monitor.wechat_source import account_paths, discover_accounts  # noqa: E402


def main():
    accounts = discover_accounts()
    assert isinstance(accounts, list)
    for path in accounts:
        assert Path(path).is_dir()
        found = account_paths(path)
        assert str(Path(path).resolve()) in found

    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        names = ['赵阳', '邱迪', '张建民']
        roster = root / '通讯录.xlsx'
        target = root / '基准.xlsx'
        make_roster(roster, names)
        make_target(target, names)
        result = update_file(
            target, roster, lambda *args: [message(FUGUO)],
            first_start='2026-10-26', end='2026-10-26 09:00:00',
            save_as_new=True, output_dir=root / 'output', records_dir=root / 'records',
        )
        assert Path(result['output']).is_file()
        assert Path(result['notes_file']).is_file()
        assert result['cleared_count'] >= 1
        assert '接龙' in result_summary(result)

    app = App()

    def verify():
        try:
            assert app.winfo_exists()
            app.output_dir.mkdir(parents=True, exist_ok=True)
            open_path(app.output_dir)
            app._connection()
            assert app.settings_window is not None and app.settings_window.winfo_exists()
            app.settings_window.update_idletasks()
            def descendants(widget):
                for child in widget.winfo_children():
                    yield child
                    yield from descendants(child)
            init_buttons = [widget for widget in descendants(app.settings_window)
                            if str(widget.winfo_class()) == 'TButton'
                            and widget.cget('text') == '自动获取密钥并连接微信']
            assert len(init_buttons) == 1
            button = init_buttons[0]
            relative_bottom = button.winfo_rooty() - app.settings_window.winfo_rooty() + button.winfo_height()
            assert relative_bottom <= app.settings_window.winfo_height(), (relative_bottom, app.settings_window.winfo_height())
            print(json.dumps({
                'gui': 'ok',
                'init_button_visible': True,
                'accounts': len(accounts),
                'connection_ok': app.connection_ok,
                'live_key_capture': 'not performed',
                'excel_path': 'ok',
                'cleared_count': result['cleared_count'],
            }, ensure_ascii=False), flush=True)
        finally:
            if app.settings_window is not None and app.settings_window.winfo_exists():
                app.settings_window.destroy()
            app.destroy()

    app.after(2000, verify)
    app.mainloop()


if __name__ == '__main__':
    main()
