"""双文件、一键更新的本地桌面入口。"""
from __future__ import annotations

import argparse
import json
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
import tkinter.font as tkfont
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .relay_core import atomic_json, load_state, now_bjt, parse_bjt, update_file, summarize_issues, latest_version, latest_run_note
from .wechat_source import WeChatSource, data_home, discover_accounts, account_paths, initialize, repair_mac_permission
from .error_messages import guide_path, record_error, record_runtime_log, diagnostics_home

GROUP = 'GH派点反馈群'


def result_summary(result):
    issues = summarize_issues(result['issues'])
    fresh = result.get('message_count', result['relay_count'])
    retried = result.get('retried_pending_count', 0)
    summary = f"本次读取 {fresh} 条接龙" + (f"，复核历史 {retried} 条" if retried else '') + '。\n'
    summary += f"清空 {result['cleared_count']} 格；{result['already_empty_count']} 格原本已空。"
    return summary + (f"另有 {len(issues)} 项待核对，见更新记录。" if issues else '')


def open_path(path):
    if sys.platform == 'darwin':
        subprocess.Popen(['open', str(path)])
    elif os.name == 'nt':
        os.startfile(str(path))
    else:
        subprocess.Popen(['xdg-open', str(path)])


class App(tk.Tk):
    def __init__(self):
        if os.name == 'nt':
            # Tk already scales point fonts for the display DPI. Avoid Windows
            # bitmap scaling the whole window a second time and clipping its footer.
            import ctypes
            ctypes.windll.user32.SetProcessDPIAware()
        super().__init__()
        self.title('群接龙更新工具')
        height = min(760, self.winfo_screenheight() - 100) if os.name == 'nt' else 680
        self.geometry(f'1000x{max(640, height)}')
        self.minsize(920, 640)
        self._setup_style()
        self.local_dir = data_home()
        self.output_dir = self.local_dir.parent / 'output'
        self.records_dir = self.local_dir.parent / 'records'
        self.events = queue.Queue()
        self.busy = False
        self.last_result = None
        self.schedule_attempt = None
        self.connection_ok = False
        self.run_until = None
        self.settings_window = None
        self.log_lines = []
        self.vars = {key: tk.StringVar() for key in ('roster', 'target', 'start', 'status', 'range', 'current', 'connection')}
        self.auto = tk.BooleanVar(value=False)
        self.vars['start'].set(now_bjt().strftime('%Y-%m-%d 00:00:00'))
        has_credentials = (self.local_dir / 'wechat.json').is_file()
        self.vars['connection'].set('正在检查已保存的微信读取凭据…' if has_credentials else '尚未连接微信，请先完成微信连接设置。')
        self._build()
        self._load_preferences()
        self.after(100, self._poll)
        if has_credentials:
            self.after(250, self._check_connection)
        self.after(1000, self._schedule)
        self._clock()
        self.protocol('WM_DELETE_WINDOW', self._close)

    def _setup_style(self):
        family = tkfont.nametofont('TkDefaultFont').actual('family')
        font_size = 11 if os.name == 'nt' else 13
        self.ui_font, self.ui_bold = (family, font_size), (family, font_size, 'bold')
        for name in ('TkDefaultFont', 'TkTextFont', 'TkMenuFont', 'TkHeadingFont'):
            tkfont.nametofont(name).configure(family=family, size=font_size)
        style = ttk.Style(self)
        style.theme_use('clam')
        self.configure(background='#F3F5F8')
        style.configure('.', font=self.ui_font, background='#F3F5F8', foreground='#253247')
        style.configure('TFrame', background='#F3F5F8')
        style.configure('TLabel', font=self.ui_font, background='#F3F5F8')
        style.configure('Title.TLabel', font=(family, 18 if os.name == 'nt' else 20, 'bold'), foreground='#17243A')
        style.configure('Muted.TLabel', foreground='#66748A')
        style.configure('Card.TFrame', background='#FFFFFF', borderwidth=1, relief='solid', bordercolor='#E1E6EE')
        style.configure('Card.TLabel', background='#FFFFFF')
        style.configure('Inner.TFrame', background='#FFFFFF')
        style.configure('Section.TLabel', background='#FFFFFF', font=self.ui_bold)
        style.configure('CardMuted.TLabel', background='#FFFFFF', foreground='#66748A')
        style.configure('TEntry', font=self.ui_font, padding=(10, 6), fieldbackground='#F8FAFC', bordercolor='#DCE2EA')
        style.map('TEntry', fieldbackground=[('readonly', '#F8FAFC'), ('disabled', '#F0F3F7')])
        style.configure('TCombobox', font=self.ui_font, padding=(8, 6))
        style.configure('TButton', font=self.ui_font, padding=(14, 7), background='#FFFFFF', foreground='#334155', borderwidth=1, bordercolor='#D9E0EA')
        style.map('TButton', background=[('active', '#EDF2F8'), ('disabled', '#F0F3F7')], foreground=[('disabled', '#9AA5B5')])
        style.configure('Primary.TButton', font=self.ui_font, background='#2563EB', foreground='#FFFFFF', bordercolor='#2563EB', padding=(20, 7))
        style.map('Primary.TButton', background=[('disabled', '#A7BFEF'), ('active', '#1D4ED8')], foreground=[('disabled', '#FFFFFF'), ('active', '#FFFFFF')])
        style.configure('Link.TButton', borderwidth=0, background='#F3F5F8', foreground='#2563EB', padding=(8, 7))
        style.map('Link.TButton', background=[('active', '#E8EEFA')])
        style.configure('TCheckbutton', font=self.ui_font, background='#F3F5F8', foreground='#526177')
        style.configure('TLabelframe.Label', font=self.ui_bold)

    def _build(self):
        frame = ttk.Frame(self, padding=20)
        frame.pack(fill='both', expand=True)
        ttk.Label(frame, text='群接龙更新', style='Title.TLabel').pack(anchor='w')
        ttk.Label(frame, text=f'{GROUP}  ·  新表统一保存到 data/output，明细保存在 data/records', style='Muted.TLabel').pack(anchor='w', pady=(6, 16))

        files = ttk.Frame(frame, style='Card.TFrame', padding=14)
        files.pack(fill='x')
        files.columnconfigure(1, weight=1)
        ttk.Label(files, text='文件选择', style='Section.TLabel').grid(row=0, column=0, columnspan=3, sticky='w', pady=(0, 10))
        self.path_entries, self.file_buttons = {}, []
        for row, key, label in [(1, 'roster', '通讯录'), (2, 'target', '更新基准')]:
            ttk.Label(files, text=label, style='Card.TLabel', width=10).grid(row=row, column=0, sticky='w', padx=(0, 12), pady=4)
            entry = ttk.Entry(files, textvariable=self.vars[key], font=self.ui_font, state='readonly')
            entry.grid(row=row, column=1, sticky='ew')
            self.path_entries[key] = entry
            button = ttk.Button(files, text='选择', command=lambda k=key: self._browse(k))
            button.grid(row=row, column=2, padx=(12, 0))
            self.file_buttons.append(button)

        period = ttk.Frame(frame, style='Card.TFrame', padding=14)
        period.pack(fill='x', pady=12)
        ttk.Label(period, text='更新信息  ·  北京时间', style='Section.TLabel').pack(anchor='w', pady=(0, 10))
        ttk.Label(period, textvariable=self.vars['range'], style='Card.TLabel', wraplength=910).pack(anchor='w')
        ttk.Label(period, textvariable=self.vars['current'], style='CardMuted.TLabel').pack(anchor='w', pady=(8, 0))
        self.start_frame = ttk.Frame(period, style='Inner.TFrame')
        ttk.Label(self.start_frame, text='首次开始时间', style='Card.TLabel').pack(side='left')
        ttk.Entry(self.start_frame, textvariable=self.vars['start'], font=self.ui_font, width=25).pack(side='left', padx=12)
        ttk.Label(self.start_frame, text='仅新文件需填写', style='CardMuted.TLabel').pack(side='left')

        results = ttk.Frame(frame, style='Card.TFrame', padding=14)
        results.pack(fill='x')
        ttk.Label(results, text='更新结果', style='Section.TLabel').pack(anchor='w', pady=(0, 10))
        ttk.Label(results, textvariable=self.vars['status'], style='Card.TLabel', wraplength=910).pack(anchor='w')

        buttons = ttk.Frame(frame)
        buttons.pack(fill='x', pady=(16, 10))
        self.run_btn = ttk.Button(buttons, text='开始更新', style='Primary.TButton', command=self._update)
        self.run_btn.pack(side='left', padx=(0, 12))
        for label, command in [('打开最新结果', self._open_target), ('查看最新记录', self._open_result), ('打开结果文件夹', self._open_folder)]:
            ttk.Button(buttons, text=label, command=command).pack(side='left', padx=(0, 8))
        ttk.Checkbutton(frame, text='保持运行时，每天 09:00、15:00 自动更新', variable=self.auto,
                        command=self._save_preferences).pack(anchor='w', pady=(0, 10))
        footer = ttk.Frame(frame)
        footer.pack(fill='x')
        ttk.Label(footer, textvariable=self.vars['connection'], style='Muted.TLabel', wraplength=700).pack(side='left')
        ttk.Button(footer, text='微信连接设置', style='Link.TButton', command=self._connection).pack(side='right')

    def _log(self, value):
        self.events.put(('log', str(value)))

    def _load_preferences(self):
        path = self.local_dir / 'preferences.json'
        if path.exists():
            try:
                cfg = json.loads(path.read_text('utf-8'))
                self.auto.set(bool(cfg.get('auto', False)))
                migration = self.local_dir / 'legacy_paths.json'
                aliases = json.loads(migration.read_text('utf-8')) if migration.exists() else {}
                for key in ('roster', 'target', 'start'):
                    if cfg.get(key):
                        self.vars[key].set(aliases.get(cfg[key], cfg[key]))
                if cfg.get('target'):
                    latest = latest_version(self.vars['target'].get(), self.output_dir)
                    if str(latest) != cfg['target']:
                        self.vars['target'].set(str(latest))
                        self._save_preferences()
                        self._log('已接续最近生成的版本：' + latest.name)
            except (OSError, ValueError) as exc:
                self._log('文件选择记录未能载入：' + str(exc))
        self._show_state()

    def _save_preferences(self):
        values = {k: self.vars[k].get() for k in ('roster', 'target', 'start')}
        values['auto'] = self.auto.get()
        atomic_json(self.local_dir / 'preferences.json', values)

    def _browse(self, key):
        path = filedialog.askopenfilename(parent=self, title='选择通讯录' if key == 'roster' else '选择要更新的 Excel', filetypes=[('Excel 文件', '*.xlsx')])
        if path:
            self.vars[key].set(path)
            self._save_preferences()
            self._show_state()

    def _show_state(self):
        self.last_result = None
        self.after_idle(self._show_file_names)
        try:
            path = self.vars['target'].get()
            state = load_state(Path(path)) if path else {}
            if state:
                self.start_frame.pack_forget()
                self.vars['range'].set('本次基于：' + Path(path).name + '\n上次成功更新：' + parse_bjt(state['through']).strftime('%Y-%m-%d %H:%M:%S'))
                detail = Path(state['last_archive']).parent / 'details.json' if state.get('last_archive') else None
                if detail and detail.is_file():
                    self.last_result = json.loads(detail.read_text('utf-8'))
                self.vars['status'].set('上次结果：' + result_summary(self.last_result) if self.last_result else '文件已有进度，直接点击“开始更新”即可。')
            else:
                self.vars['range'].set('本次基于：' + (Path(path).name if path else '尚未选择') + '\n上次成功更新：无记录，请确认首次开始时间。')
                self.start_frame.pack(fill='x', pady=(8, 0))
                self.vars['status'].set('选好文件后点击开始更新，将另存带时间的新版本。')
        except (OSError, ValueError, KeyError) as exc:
            self.vars['status'].set('进度读取失败，请查看“微信连接设置”：' + str(exc))

    def _show_file_names(self):
        for entry in self.path_entries.values():
            entry.icursor('end')
            entry.xview_moveto(1)

    def _clock(self):
        current = self.run_until or now_bjt()
        self.vars['current'].set(('本次更新截止：' if self.run_until else '当前时间 / 本次截止：') + current.strftime('%Y-%m-%d %H:%M:%S') + '（北京时间）')
        self.after(1000, self._clock)

    def _work(self, fn, kind='done', operation='读取接龙并更新 Excel', *, automatic=False):
        if self.busy:
            return
        self.busy = True
        def run():
            try:
                self.events.put((kind, fn()))
            except Exception as exc:
                event = 'connection_error' if kind == 'checked' else ('automatic_error' if automatic else 'error')
                self.events.put((event, record_error(self.local_dir, exc, operation)))
            finally:
                self.events.put(('idle', None))
        try:
            for widget in [self.run_btn, *self.file_buttons, *self.path_entries.values()]:
                widget.configure(state='disabled')
            threading.Thread(target=run, daemon=True).start()
        except Exception as exc:
            self.events.put(('automatic_error' if automatic else 'error', record_error(self.local_dir, exc, operation)))
            self.events.put(('idle', None))

    def _check_connection(self):
        self._work(lambda: WeChatSource(self.local_dir).inspect(), 'checked', '检查已保存的微信读取凭据')

    def _update(self, *, automatic=False):
        if self.busy:
            return
        target, roster, start = (self.vars[k].get().strip() for k in ('target', 'roster', 'start'))
        try:
            if not Path(target).is_file() or not Path(roster).is_file():
                raise ValueError('UPDATE_FILES_MISSING: 请选择通讯录和要更新的 Excel。')
            if not load_state(Path(target)):
                parse_bjt(start)
            self._save_preferences()
        except Exception as exc:
            self.events.put(('automatic_error' if automatic else 'error', record_error(self.local_dir, exc, '更新前检查文件、时间和配置')))
            return
        self.run_until = now_bjt()
        self.vars['current'].set('本次更新截止：' + self.run_until.strftime('%Y-%m-%d %H:%M:%S') + '（北京时间）')
        self.vars['status'].set(f'正在基于 {Path(target).name} 生成新版本，请稍候…')
        source = WeChatSource(self.local_dir, log=self._log)
        cutoff = self.run_until
        self._work(lambda: update_file(target, roster, source.fetch, first_start=start, end=cutoff, group=GROUP, log=self._log, save_as_new=True, output_dir=self.output_dir, records_dir=self.records_dir), automatic=automatic)

    def _poll(self):
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == 'log':
                    line = f'[{now_bjt():%H:%M:%S}] {value}\n'
                    self.log_lines.append(line)
                    self.log_lines = self.log_lines[-1000:]
                    record_runtime_log(self.local_dir, line)
                elif kind == 'done':
                    self.vars['target'].set(value.get('output', value['source']))
                    try:
                        self._save_preferences()
                    except OSError as exc:
                        self._log('新文件已保存，但默认路径未能保存：' + str(exc))
                    try:
                        atomic_json(diagnostics_home(self.local_dir) / 'latest_update.json', value)
                    except OSError:
                        self._log('排错副本未能保存；本轮 TXT 和逐轮原始明细仍保留。')
                    self._show_state()
                    self.connection_ok = True
                    self.vars['connection'].set(f'微信已就绪：{GROUP}。日常无需初始化。')
                    self.vars['status'].set('更新完成。' + result_summary(value))
                elif kind == 'checked':
                    self.connection_ok = True
                    self.vars['connection'].set(f'微信已就绪：{GROUP}，可直接更新。')
                    self._log('微信读取正常，已找到目标群。无需重新初始化。')
                elif kind == 'initialized':
                    self._log('读取凭据已保存，正在检查是否可以使用。')
                    self.after_idle(self._check_connection)
                elif kind == 'repaired':
                    self.vars['connection'].set('读取权限已修复；请重新打开微信并登录，再完成首次设置。')
                    self._log('权限修复完成，请按提示重新登录。')
                elif kind == 'connection_error':
                    self.connection_ok = False
                    self.vars['connection'].set('微信暂不可读取，请点“微信连接设置”。')
                    self._log(f"{value['code']}：{value['summary']}\n{value['action']}\n{value['error']}")
                elif kind in ('error', 'automatic_error'):
                    self.vars['status'].set(f"更新未完成 [{value['code']}]：{value['summary']}\n{value['action']}")
                    self._log(json.dumps(value, ensure_ascii=False, indent=2))
                    if kind == 'error':
                        messagebox.showerror('操作未完成', value['summary'] + '\n\n' + value['action'] + '\n\n具体错误已保存在data/diagnostics 文件夹。\n错误编号：' + value['code'], parent=self)
                elif kind == 'idle':
                    self.busy = False
                    self.run_until = None
                    for widget in [self.run_btn, *self.file_buttons]:
                        widget.configure(state='normal')
                    for entry in self.path_entries.values():
                        entry.configure(state='readonly')
        except queue.Empty:
            pass
        except Exception as exc:
            report = record_error(self.local_dir, exc, '处理后台任务结果')
            self.vars['status'].set(f"[{report['code']}] {report['summary']} {report['action']}")
            self._log(json.dumps(report, ensure_ascii=False))
        finally:
            self.after(100, self._poll)

    def _schedule(self):
        try:
            current = now_bjt()
            slot = current.strftime('%Y-%m-%d %H')
            connection = getattr(self, 'settings_window', None)
            connecting = getattr(connection, 'waiting', False) or getattr(connection, 'connecting', False)
            if self.auto.get() and current.hour in (9, 15) and current.minute < 5 and slot != self.schedule_attempt and not self.busy and not connecting:
                self.schedule_attempt = slot
                self._log('北京时间定时更新开始。')
                self._update(automatic=True)
        except Exception as exc:
            self.events.put(('automatic_error', record_error(self.local_dir, exc, '北京时间定时更新')))
        finally:
            self.after(10000, self._schedule)

    def _connection(self):
        if self.settings_window is not None and self.settings_window.winfo_exists():
            self.settings_window.lift()
            return
        if os.name == 'nt':
            from .connection_wizard import WindowsConnection
            self.settings_window = WindowsConnection(self)
            return
        window = self.settings_window = tk.Toplevel(self)
        window.title('微信连接设置')
        window.geometry('900x560' if os.name == 'nt' else '900x420')
        box = ttk.Frame(window, padding=18)
        box.pack(fill='both', expand=True)
        ttk.Label(box, textvariable=self.vars['connection'], font=self.ui_bold, wraplength=700).pack(anchor='w')
        ttk.Label(box, text='已有有效凭据时，返回主界面点击“开始更新”即可。首次使用或更换账号时，选择与实际登录账号一致的数据目录，再完成一次初始化。', wraplength=700).pack(anchor='w', pady=12)
        accounts = discover_accounts()
        try:
            selected = WeChatSource(self.local_dir).config().get('db_dir', '')
        except (OSError, ValueError):
            selected = accounts[0] if len(accounts) == 1 else ''
        account = tk.StringVar(value=selected)
        account_hint = tk.StringVar()
        ttk.Label(box, text='微信数据目录', font=self.ui_bold).pack(anchor='w', pady=(18, 4))
        picker = ttk.Combobox(box, textvariable=account, values=accounts)
        picker.pack(fill='x', pady=(0, 6))
        def hint():
            text = f'自动发现 {len(accounts)} 个账号目录。' + ('有多个候选，请核对当前登录账号；目录名不一定等于微信号。' if len(accounts) > 1 else '未识别到时，请手动选择微信文件存储位置或账号文件夹。')
            if account.get().strip():
                directory = Path(account.get().strip())
                try:
                    files = list((directory / 'message').glob('message_*.db'))
                    if files:
                        from datetime import datetime
                        modified = datetime.fromtimestamp(max(p.stat().st_mtime for p in files)).strftime('%Y-%m-%d %H:%M')
                        text += f'\n当前选择：{directory.parent.name}；消息库最近修改：{modified}（仅供核对，不代表当前登录账号）。'
                except OSError:
                    pass
            account_hint.set(text)
        account.trace_add('write', lambda *_: hint())
        def rescan():
            nonlocal accounts
            accounts = discover_accounts()
            picker.configure(values=accounts)
            if not account.get() and len(accounts) == 1:
                account.set(accounts[0])
            hint()
        def choose():
            value = filedialog.askdirectory(parent=window, title='选择微信文件存储位置、账号文件夹或 db_storage')
            if not value:
                return
            paths = account_paths(value)
            if not paths:
                messagebox.showinfo('未找到账号数据', '该文件夹没有找到 db_storage。请在微信设置中查看文件存储位置，或直接选择当前账号的 db_storage 文件夹；若系统拒绝访问，请检查完全磁盘访问权限。', parent=window)
                return
            nonlocal accounts
            accounts = sorted(set(accounts + paths))
            picker.configure(values=accounts)
            if len(paths) == 1:
                account.set(paths[0])
                hint()
            else:
                account.set('')
                account_hint.set(f'所选位置找到 {len(paths)} 个账号，请从下拉列表选择当前登录账号。')
        path_actions = ttk.Frame(box)
        path_actions.pack(anchor='w')
        ttk.Button(path_actions, text='重新识别', command=rescan).pack(side='left', padx=(0, 8))
        ttk.Button(path_actions, text='选择文件夹', command=choose).pack(side='left')
        ttk.Label(box, textvariable=account_hint, wraplength=850).pack(anchor='w', pady=(6, 0))
        hint()
        ttk.Label(box, text='选择路径只设置候选目录；已有凭据继续使用保存的账号。更换账号后需完成下方一次性初始化。', wraplength=850).pack(anchor='w', pady=(6, 0))
        advanced = ttk.Frame(box)
        def toggle():
            if advanced.winfo_manager():
                advanced.pack_forget()
                window.geometry('900x420')
            else:
                advanced.pack(fill='x', pady=12)
                window.geometry('900x690')
        if sys.platform == 'darwin':
            ttk.Button(box, text='高级维护（首次设置或更换账号时展开）', command=toggle).pack(anchor='w', pady=16)
        def init(capture=False):
            paths = account_paths(account.get().strip()) if account.get().strip() else []
            if len(paths) != 1:
                messagebox.showinfo('先选择账号', '请选择唯一的当前登录账号目录；可以点击“选择文件夹”定位 db_storage。', parent=window)
                return
            directory = paths[0]
            account.set(directory)
            try:
                current = WeChatSource(self.local_dir).config().get('db_dir')
            except (OSError, ValueError):
                current = None
            if self.connection_ok and directory == current:
                messagebox.showinfo('无需初始化', '当前微信已可读取，直接回主界面点“开始更新”即可。', parent=window)
                return
            if capture or os.name == 'nt':
                if sys.platform == 'darwin':
                    prompt = '沿用 gh_ui 的一次性调试器抓取，可能触发微信安全验证。仅在普通初始化无结果时使用。启动后请打开会话、滚动消息触发读取。是否继续？'
                else:
                    prompt = ('沿用 gh_ui 的一次性初始化：请先从托盘完全退出微信。'
                              '允许管理员授权后，在弹出的微信窗口登录所选目录对应账号。'
                              '此步骤可能触发微信安全验证；成功后日常读取无需再次执行。是否继续？')
                if not messagebox.askyesno('连接微信', prompt, parent=window):
                    return
            use_capture = True if os.name == 'nt' else capture
            self._work(lambda: initialize(directory, self.local_dir, self._log, capture=use_capture),
                       'initialized', '连接微信初始化' if os.name == 'nt' else ('新版微信一次性初始化' if capture else '普通初始化'))
        if sys.platform == 'darwin':
            ttk.Label(advanced, text='首次没有凭据：先尝试普通初始化。').pack(anchor='w', pady=(12, 4))
            ttk.Button(advanced, text='普通初始化', command=init).pack(anchor='w')
            ttk.Label(advanced, text='普通扫描无结果：才使用一次性新版初始化。', wraplength=850).pack(anchor='w', pady=(12, 4))
            ttk.Button(advanced, text='新版微信初始化', command=lambda: init(True)).pack(anchor='w')
            def repair():
                if self.connection_ok:
                    messagebox.showinfo('无需修复', '当前微信已可读取，无需修改读取权限。', parent=window)
                elif messagebox.askyesno('修改微信签名以修复进程读取权限', '仅用于进程权限错误，不能解决文件夹访问被拒绝。请先正常退出微信；将沿用 gh_ui 替换微信应用签名并添加调试读取权限，需管理员授权，可能影响微信自动更新。是否继续？', parent=window):
                    self._work(repair_mac_permission, 'repaired', '修复 Mac 读取权限')
            ttk.Button(advanced, text='修复进程读取权限（会修改微信签名）', command=repair).pack(anchor='w', pady=12)
        else:
            ttk.Label(box, text='首次连接：从托盘完全退出微信，再点下方按钮。工具自动获取并保存密钥；请在弹出的微信窗口完成一次登录。', wraplength=850).pack(anchor='w', pady=(8, 4))
            ttk.Button(box, text='自动获取密钥并连接微信', command=lambda: init(True), style='Primary.TButton').pack(anchor='w')

    def _open_target(self):
        versions = []
        for path in self.output_dir.glob('*.xlsx'):
            state = load_state(path)
            if state:
                versions.append((state.get('through', ''), path))
        if versions:
            open_path(max(versions, key=lambda x: (x[0], x[1].name))[1])
        else:
            messagebox.showinfo('暂无更新结果', '完成第一次更新后，这里会打开最新生成的 Excel。', parent=self)

    def _open_folder(self):
        self.output_dir.mkdir(parents=True, exist_ok=True)
        open_path(self.output_dir)

    def _open_result(self):
        notes = latest_run_note(self.records_dir)
        if notes is None:
            messagebox.showinfo('暂无更新记录', '成功更新后可在这里查看更新前备份、结果和明细。', parent=self)
            return
        open_path(notes)

    def _close(self):
        if self.busy:
            messagebox.showinfo('正在运行', '请等待当前操作完成后关闭。', parent=self)
        else:
            self._save_preferences()
            self.destroy()


def main():
    parser = argparse.ArgumentParser(description='群接龙更新工具')
    parser.add_argument('--check-runtime', action='store_true', help='检查打包后的微信初始化依赖，不读取真实微信')
    parser.add_argument('--setup', action='store_true', help='打开首次使用向导，连接操作由 GUI 完成')
    parser.add_argument('--inspect-wechat', action='store_true')
    parser.add_argument('--initialize-wechat', action='store_true', help='显式执行一次 gh_ui 微信初始化')
    parser.add_argument('--db-dir', type=Path, help='初始化使用的当前账号 db_storage')
    parser.add_argument('--update', action='store_true')
    parser.add_argument('--roster')
    parser.add_argument('--target')
    parser.add_argument('--start', help='首次开始时间，北京时间')
    parser.add_argument('--local-data', type=Path)
    args = parser.parse_args()
    if args.check_runtime:
        import importlib
        modules = ['wechat_native.scanner_mac', 'wechat_native.scanner_win', 'wechat_native.active_key',
                   'wechat_native.crypto', 'wechat_native.key_scan', 'plistlib', 'ctypes.util', 'shlex', 'zstandard']
        for name in modules:
            importlib.import_module(name)
        from wechat_native import scanner_mac
        from wechat_native.active_key import wechat_key_script
        assert scanner_mac._helper_path().is_file()
        assert scanner_mac._lldb_capture_path().is_file()
        assert wechat_key_script() and wechat_key_script().is_file()
        assert guide_path().is_file()
        print(json.dumps({'status': 'ok', 'modules': modules, 'check': 'dependencies only; no live access',
                          'helper_scripts': 'present', 'windows_key_script': 'present',
                          'troubleshooting_guide': 'present'}, ensure_ascii=False, indent=2))
    elif args.initialize_wechat:
        home = args.local_data or data_home()
        def log(message):
            from .error_messages import redact
            line = redact(message)
            record_runtime_log(home, line + '\n')
            print(line, flush=True)
        try:
            if not args.db_dir:
                raise ValueError('请先选择微信账号的数据目录。')
            count = initialize(args.db_dir, home, log, capture=True)
            print(json.dumps({'initialized': True, 'key_count': count}, ensure_ascii=False), flush=True)
        except Exception as exc:
            report = record_error(home, exc, 'Windows 一次性密钥初始化')
            print(f"[{report['code']}] {report['summary']}\n{report['action']}\n{report['error']}", file=sys.stderr, flush=True)
            raise SystemExit(1)
    elif args.inspect_wechat:
        print(json.dumps(WeChatSource(args.local_data).inspect(), ensure_ascii=False, indent=2))
    elif args.update:
        home = args.local_data or data_home()
        result = update_file(args.target, args.roster, WeChatSource(home, log=print).fetch, first_start=args.start, log=print, save_as_new=True, output_dir=home.parent/'output', records_dir=home.parent/'records')
        print(json.dumps({k: result[k] for k in ('updated_at', 'cleared_count', 'output', 'archive')}, ensure_ascii=False, indent=2))
    else:
        app = App()
        if args.setup:
            app.after(700, app._connection)
        app.mainloop()


if __name__ == '__main__':
    main()
