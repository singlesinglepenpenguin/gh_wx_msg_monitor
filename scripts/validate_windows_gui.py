"""Exercise the real Tk buttons with saved WeChat credentials and isolated Excel files."""
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from group_msg_monitor.app import App
from group_msg_monitor.error_messages import record_error
from group_msg_monitor.relay_core import BJT, file_hash, latest_run_note, load_state, summarize_issues


def main():
    stamp = datetime.now(BJT).strftime('%Y%m%d_%H%M%S')
    work = ROOT / 'data/validation' / ('gui_' + stamp)
    local = work / 'local_data'
    local.mkdir(parents=True)
    shutil.copy2(ROOT / 'data/local_data/wechat.json', local / 'wechat.json')
    (local / 'wechat.json').chmod(0o600)
    baseline = ROOT / 'data/reference/要分名单 26Q3.xlsx'
    roster = ROOT / 'data/reference/研究所内部通讯录9.10.xlsx'
    before_hash = file_hash(baseline)
    target = work / 'baseline.xlsx'
    shutil.copy2(baseline, target)
    report = {'synthetic': False, 'initialization_requested': False, 'stage': 'gui_connection'}
    report_path = ROOT / 'data/diagnostics/windows_live_gui.json'
    app = App()
    app.geometry('+0+0')
    app.local_dir = local
    app.output_dir = work / 'output'
    app.records_dir = work / 'records'
    app.auto.set(False)
    app.vars['target'].set(str(target))
    app.vars['roster'].set(str(roster))
    app.vars['start'].set('2026-10-26 00:00:00')
    app._show_state()
    deadline = time.monotonic() + 180
    started = False
    errors = []

    def save():
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

    def capture():
        # Capture only this application's client area while it is visible.
        app.update_idletasks()
        visible_bottom = app.winfo_rooty() + app.winfo_height()
        def check_layout(parent):
            for widget in parent.winfo_children():
                if widget is app.start_frame and not widget.winfo_manager():
                    continue  # Intentionally hidden once the workbook has progress.
                if (not widget.winfo_ismapped() or widget.winfo_height() < widget.winfo_reqheight()
                        or widget.winfo_rooty() + widget.winfo_height() > visible_bottom):
                    raise RuntimeError('WINDOWS_DISPLAY_LAYOUT: clipped control ' + str(widget))
                check_layout(widget)
        check_layout(app)
        report['all_main_controls_visible'] = True
        environment = dict(os.environ)
        screenshot = ROOT / 'data/diagnostics/windows_gui_success.png'
        environment['GMM_CAPTURE_FILE'] = str(screenshot)
        # DWM reports physical pixels; Tk coordinates can be virtualized by
        # Windows display scaling and would crop the screenshot at 125% DPI.
        code = (
            'Add-Type -AssemblyName System.Drawing; '
            'Add-Type -TypeDefinition \'using System; using System.Runtime.InteropServices; '
            'public class AcceptanceFrame { [DllImport("user32.dll")] '
            'public static extern IntPtr GetAncestor(IntPtr h,uint flags); '
            '[DllImport("dwmapi.dll")] public static extern int DwmGetWindowAttribute('
            'IntPtr h,uint attribute,[Out] int[] rectangle,int size); }\'; '
            f'$handle=[AcceptanceFrame]::GetAncestor([IntPtr]{app.winfo_id()},2); '
            '$rect=New-Object int[] 4; '
            '$status=[AcceptanceFrame]::DwmGetWindowAttribute($handle,9,$rect,16); '
            'if ($status -ne 0) { throw "Cannot locate application frame" }; '
            '$bitmap=New-Object System.Drawing.Bitmap(($rect[2]-$rect[0]),($rect[3]-$rect[1])); '
            '$graphics=[System.Drawing.Graphics]::FromImage($bitmap); '
            'try { '
            '$graphics.CopyFromScreen($rect[0],$rect[1],0,0,$bitmap.Size); '
            '$bitmap.Save($env:GMM_CAPTURE_FILE) '
            '} finally { $graphics.Dispose(); $bitmap.Dispose() }'
        )
        captured = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', code],
                                  env=environment, capture_output=True, timeout=20,
                                  creationflags=subprocess.CREATE_NO_WINDOW)
        if captured.returncode:
            report['screenshot_error'] = captured.stderr.decode('utf-8', errors='replace')
        else:
            report['screenshot'] = str(screenshot)
        save()
        print(json.dumps(report, ensure_ascii=False), flush=True)
        app.destroy()

    def capture_and_finish():
        try:
            capture()
        except Exception as exc:
            errors.append(exc)
            failure = record_error(ROOT / 'data/local_data', exc, 'Windows GUI layout and screenshot')
            report.update(status='failed', error=str(exc), error_code=failure['code'])
            save()
            app.destroy()

    def poll():
        nonlocal started
        try:
            if time.monotonic() > deadline:
                raise TimeoutError('Real GUI acceptance did not finish in 180 seconds')
            if not app.busy and app.connection_ok and not started:
                app._connection()
                assert app.settings_window.winfo_exists()
                app.settings_window.destroy()
                app.settings_window = None
                report.update(stage='gui_update', connection_ok=True)
                save()
                started = True
                app.run_btn.invoke()
            elif started and not app.busy:
                assert app.vars['status'].get().startswith('更新完成。'), app.vars['status'].get()
                result = app.last_result
                assert result and result['message_count'] > 0
                output = Path(result['output'])
                assert output.is_file() and load_state(output)
                assert str(output) == app.vars['target'].get()
                assert file_hash(baseline) == before_hash
                report.update(status='passed', stage='complete', output=str(output),
                              notes_file=str(latest_run_note(app.records_dir)), message_count=result['message_count'],
                              cleared_count=result['cleared_count'],
                              issues_to_review=len(summarize_issues(result['issues'])),
                              ui_status=app.vars['status'].get(), connection=app.vars['connection'].get(),
                              next_version_selected=True, reference_unchanged=True)
                save()
                app.lift()
                app.attributes('-topmost', True)
                app.after(1000, capture_and_finish)
                return
            app.after(200, poll)
        except Exception as exc:
            errors.append(exc)
            failure = record_error(ROOT / 'data/local_data', exc, 'Real Windows GUI acceptance')
            report.update(status='failed', error=str(exc), error_code=failure['code'])
            save()
            app.destroy()

    save()
    app.after(500, poll)
    app.mainloop()
    if errors:
        raise errors[0]


if __name__ == '__main__':
    main()
