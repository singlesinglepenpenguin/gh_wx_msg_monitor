"""PyInstaller entry point; source users run python -m group_msg_monitor."""
import sys

if __name__ == '__main__':
    try:
        from group_msg_monitor.app import main
        main()
    except Exception as exc:
        from group_msg_monitor.paths import data_home
        from group_msg_monitor.error_messages import record_error
        report = record_error(data_home(), exc, '应用启动或运行时依赖检查')
        cli_modes = {'--check-runtime', '--inspect-wechat', '--initialize-wechat', '--update'}
        if not cli_modes.intersection(sys.argv[1:]):
            from tkinter import messagebox
            messagebox.showerror('启动未完成', f"[{report['code']}] {report['summary']}\n\n{report['action']}")
        raise SystemExit(1)
