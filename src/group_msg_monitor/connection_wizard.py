"""The customer-facing Windows first-connection flow; upstream acquisition is unchanged."""
from __future__ import annotations

import queue
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from .error_messages import record_error
from .wechat_source import WeChatSource, account_paths, discover_accounts, initialize
from .windows_client import account_in_use, open_wechat, request_normal_exit


def connect_and_verify(directory, local_dir, log, on_verifying=lambda: None):
    initialize(directory, local_dir, log, capture=True)
    on_verifying()
    inspected = WeChatSource(local_dir).inspect()
    log('一次性连接完成，正在正常打开微信，供日常同步新消息。')
    open_wechat()  # Ordinary startup, never another extraction/debugger.
    return inspected


class WindowsConnection(tk.Toplevel):
    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.title('微信连接设置')
        self.geometry('820x570')
        self.minsize(760, 540)
        self.entries = {}
        self.selected = tk.StringVar()
        self.status = tk.StringVar(value='请先登录要测试的微信，再点击“识别当前账号”。')
        self.detail = tk.StringVar()
        self.stage = tk.StringVar(value='1. 识别账号 → 2. 一次性连接 → 3. 日常直接更新')
        self.progress = queue.Queue()
        self.waiting = False
        self.connecting = False
        self.pending_directory = None
        self.started_at = None
        box = ttk.Frame(self, padding=20)
        box.pack(fill='both', expand=True)
        ttk.Label(box, text='连接本机微信', font=app.ui_bold).pack(anchor='w')
        ttk.Label(box, textvariable=self.stage, wraplength=760).pack(anchor='w', pady=(12, 8))
        ttk.Label(box, text='首次连接会保存本机读取凭据。以后保持微信登录，直接点击主界面的“开始更新”。', wraplength=760).pack(anchor='w', pady=(0, 12))
        ttk.Label(box, text='使用的账号', font=app.ui_bold).pack(anchor='w')
        self.picker = ttk.Combobox(box, state='readonly', textvariable=self.selected)
        self.picker.pack(fill='x', pady=(6, 8))
        actions = ttk.Frame(box)
        actions.pack(anchor='w')
        self.rescan = ttk.Button(actions, text='识别当前账号', command=self.identify)
        self.rescan.pack(side='left', padx=(0, 8))
        self.open_button = ttk.Button(actions, text='打开微信', command=self.open_client)
        self.open_button.pack(side='left', padx=(0, 8))
        self.manual = ttk.Button(actions, text='未找到账号？选择文件夹', command=self.choose)
        self.manual.pack(side='left')
        ttk.Label(box, textvariable=self.detail, wraplength=760, style='Muted.TLabel').pack(anchor='w', pady=(8, 14))
        ttk.Separator(box).pack(fill='x')
        ttk.Label(box, textvariable=self.status, wraplength=760).pack(anchor='w', pady=14)
        self.bar = ttk.Progressbar(box, mode='indeterminate')
        self.bar.pack(fill='x', pady=(0, 12))
        self.connect_button = ttk.Button(box, text='自动获取密钥并连接微信', style='Primary.TButton', command=self.connect)
        self.connect_button.pack(anchor='w')
        self.finish_button = ttk.Button(box, text='完成，返回主界面', command=self.close)
        self.finish_button.pack(anchor='w', pady=(10, 0))
        self.protocol('WM_DELETE_WINDOW', self.close)
        # A fresh setup starts empty. Account discovery is explicit; saved,
        # verified credentials retain the normal daily-use experience.
        if self.app.connection_ok:
            self.after(100, self.identify)
        self.after(200, self.poll)

    def identify(self):
        if self.connecting or self.waiting or self.app.busy:
            self.after(300, self.identify)
            return
        accounts = discover_accounts()
        active = account_in_use(accounts)
        self.entries = {}
        chosen = None
        try:
            cfg = WeChatSource(self.app.local_dir).config()
            configured = cfg.get('db_dir')
        except (OSError, ValueError):
            configured = None
        for i, directory in enumerate(accounts, 1):
            if directory in active:
                label = '当前正在使用的微信账号' if len(active) == 1 else f'正在使用的微信账号 {i}'
            elif directory == configured:
                label = '已保存读取凭据的账号'
            else:
                label = f'本机历史账号 {i}（请确认后选择）'
            self.entries[label] = directory
            if directory == configured and self.app.connection_ok:
                chosen = label
            elif len(active) == 1 and directory in active and chosen is None:
                chosen = label
        self.picker.configure(values=list(self.entries))
        self.selected.set(chosen or (next(iter(self.entries)) if len(accounts) == 1 else ''))
        if self.app.connection_ok:
            self.status.set('微信已就绪。返回主界面直接更新，无需再次获取密钥。')
            self.stage.set('连接已完成 · 日常无需初始化')
        elif len(active) == 1:
            self.status.set('已通过 Windows 文件占用确认当前账号。点击下方按钮开始一次性连接。')
        else:
            self.status.set('请先在微信登录要读取的账号，再点“识别当前账号”。不会按目录排序猜选账号。')
        self.detail.set(f'发现 {len(accounts)} 个本机账号；确认 {len(active)} 个账号正在被微信使用。无需填写路径或密钥。')

    def choose(self):
        value = filedialog.askdirectory(parent=self, title='选择微信设置中的文件存储位置')
        if not value:
            return
        paths = account_paths(value)
        if not paths:
            messagebox.showinfo('未找到微信账号', '所选文件夹没有找到微信账号数据。请先登录微信，再点“识别当前账号”。', parent=self)
            return
        for index, path in enumerate(paths, 1):
            self.entries[f'手动选择的本机账号 {index}'] = path
        self.picker.configure(values=list(self.entries))
        if len(paths) == 1:
            self.selected.set(f'手动选择的本机账号 1')

    def open_client(self):
        try:
            open_wechat()
            self.status.set('请在微信中完成本人登录，再回到这里点“识别当前账号”。')
        except Exception as exc:
            failure = record_error(self.app.local_dir, exc, '打开本机微信')
            messagebox.showerror('无法打开微信', f"[{failure['code']}] {failure['summary']}\n{failure['action']}", parent=self)

    def connect(self):
        if self.app.busy or self.connecting or self.waiting:
            return
        directory = self.entries.get(self.selected.get())
        if not directory:
            messagebox.showinfo('先登录微信', '请在微信登录目标账号，再点击“识别当前账号”。无需提供密钥或输入数据路径。', parent=self)
            return
        try:
            cfg = WeChatSource(self.app.local_dir).config()
        except (OSError, ValueError):
            cfg = {}
        if self.app.connection_ok and cfg.get('db_dir') == directory:
            self.status.set('当前账号已经连接，可直接返回主界面更新。')
            return
        prompt = ('将为所选账号完成一次性连接，可能显示 Windows 管理员授权和微信安全验证。'
                  '\n请先等待微信同步完成并保存未发送的草稿。工具将请求微信正常退出；不会强制结束进程。'
                  '\n随后请在新微信窗口登录同一账号。成功后日常不再重复取钥。是否继续？')
        if not messagebox.askyesno('首次连接微信', prompt, parent=self):
            return
        self.pending_directory = directory
        self.app.connection_ok = False
        self.waiting = True
        self.set_controls(False)
        self.bar.start(15)
        self.stage.set('2. 准备一次性连接')
        self.status.set('正在请求微信正常退出…')
        def prepare():
            try:
                request_normal_exit(self.app._log)
            except RuntimeError as exc:
                # A refusal is not permission to force-kill or change upstream.
                record_error(self.app.local_dir, exc, '连接前请求微信正常退出')
                self.progress.put(('exit_required', str(exc)))
            self.progress.put(('wait_for_exit', None))
        import threading
        threading.Thread(target=prepare, daemon=True).start()

    def set_controls(self, enabled):
        state = 'normal' if enabled else 'disabled'
        for button in (self.connect_button, self.rescan, self.manual, self.open_button):
            button.configure(state=state)
        self.picker.configure(state='readonly' if enabled else 'disabled')
        for widget in (self.app.run_btn, *self.app.file_buttons):
            widget.configure(state=state)

    def wait_for_exit(self):
        from wechat_native.scanner_win import _list_running_weixin_pids
        if not self.winfo_exists() or not self.waiting:
            return
        if _list_running_weixin_pids():
            self.status.set('微信尚未退出。请右键任务栏右下角微信图标 → 退出（不是窗口右上角的 ×）。\n退出后工具自动继续，无需重新点击连接。')
            self.after(700, self.wait_for_exit)
            return
        self.waiting = False
        self.connecting = True
        self.started_at = time.monotonic()
        self.status.set('请允许 Windows 管理员授权，并在新微信窗口登录刚才确认的同一账号。取钥时“正在进入”可能短暂停留，请勿重复点击。')
        directory = self.pending_directory
        def run():
            return connect_and_verify(directory, self.app.local_dir, self.app._log,
                                      lambda: self.progress.put(('verifying', None)))
        self.app._work(run, 'checked', 'GUI 一次性连接微信并核验目标群')

    def poll(self):
        try:
            while True:
                kind, value = self.progress.get_nowait()
                if kind == 'exit_required':
                    self.status.set('微信未接受正常退出请求，请从微信自己的托盘菜单退出；工具会自动检测并继续。')
                elif kind == 'wait_for_exit':
                    self.wait_for_exit()
                elif kind == 'verifying':
                    self.stage.set('3. 凭据已保存，正在核验群连接')
        except queue.Empty:
            pass
        if self.connecting and not self.app.busy:
            self.connecting = False
            self.bar.stop()
            self.set_controls(True)
            if self.app.connection_ok:
                self.stage.set('连接完成 · 日常无需再次获取密钥')
                self.detail.set('本机读取凭据已保存并验证。日常读取复用凭据，不再启动取钥程序。')
                self.status.set('已保存读取凭据并找到目标群，也已正常打开微信。请完成本人登录并保持运行，以便同步新消息。\n点击“完成，返回主界面”，选择通讯录和更新基准后开始更新。')
            else:
                self.stage.set('连接未完成 · 可按错误编号处理后重试')
                self.status.set('请按刚才的错误提示处理。错误编号和处理办法已保存在本机，已有凭据不会被清空。')
        elif self.connecting and self.started_at:
            self.detail.set(f'正在等待授权、本人登录或校验数据库，已用时 {int(time.monotonic() - self.started_at)} 秒。请勿重复初始化。')
        self.after(200, self.poll)

    def close(self):
        if self.connecting:
            messagebox.showinfo('正在连接', '请等待本次连接完成后关闭；不要重复启动取钥。', parent=self)
            return
        self.waiting = False
        if not self.app.busy:
            self.set_controls(True)
        self.app.settings_window = None
        self.destroy()
