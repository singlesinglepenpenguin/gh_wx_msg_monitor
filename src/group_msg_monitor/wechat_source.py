"""沿用 gh_ui 的微信本地数据读取实现，不依赖其 FastAPI/金融模块。"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
from pathlib import Path

from .paths import PROJECT_ROOT, resource_root, data_home

BASE = resource_root()
sys.path.insert(0, str(BASE / 'vendor'))
from wechat_native import messages as native
from wechat_native.key_scan import collect_db_files, keys_from_passphrase, verify_enc_key

from .relay_core import BJT, atomic_json
from datetime import datetime


def account_paths(folder):
    """只定位目录，不读取密钥、不初始化；允许选择账号或文件存储根目录。"""
    root = Path(folder).expanduser()
    if not root.is_dir():
        return []
    if root.name == 'db_storage':
        return [str(root.resolve())]
    found = []
    for pattern in ('db_storage', '*/db_storage', 'xwechat_files/*/db_storage'):
        found.extend(str(p.resolve()) for p in root.glob(pattern) if p.is_dir())
    return sorted(set(found))


def discover_accounts():
    home = Path.home()
    roots = []
    if sys.platform == 'darwin':
        roots = [home / 'Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files',
                 home / 'Library/Containers/com.tencent.xinWeChat/Data/Library/Application Support/com.tencent.xinWeChat',
                 home / 'Library/Application Support/com.tencent.xinWeChat']
    elif os.name == 'nt':
        roots = [home / 'xwechat_files', home / 'Documents/xwechat_files']
        appdata = Path(os.environ.get('APPDATA', home)) / 'Tencent/xwechat'
        # 沿用 gh_ui 从 ini 定位自定义文件根目录的思路，不扫描全盘。
        if appdata.exists():
            for ini in appdata.rglob('*.ini'):
                for enc in ('utf-8', 'gbk'):
                    try:
                        for line in ini.read_text(encoding=enc).splitlines():
                            text = line.split('=', 1)[-1].strip().strip('"')
                            if re.match(r'^[A-Za-z]:[\\/]', text):
                                roots.extend([Path(text), Path(text) / 'xwechat_files'])
                        break
                    except (OSError, UnicodeError):
                        pass
    found = []
    for root in roots:
        if root.exists():
            paths = account_paths(root)
            if not paths and sys.platform == 'darwin':
                # 与 gh_ui 一样，旧版布局浅层未命中时检查该已知根目录。
                paths = [str(p.resolve()) for p in root.rglob('db_storage') if p.is_dir()]
            found.extend(paths)
    return sorted(set(found))


def existing_configs():
    home = Path.home()
    return [PROJECT_ROOT.parents[1] / 'gh_ui/data/wechat',
            home / 'Library/Application Support/cn.guohai.quant-terminal/wechat',
            Path(os.environ.get('APPDATA', home)) / 'cn.guohai.quant-terminal/wechat']


def import_gh_config(config_dir, local_dir):
    config_dir, local_dir = Path(config_dir), Path(local_dir)
    cfg = json.loads((config_dir / 'config.json').read_text('utf-8'))
    key_file = config_dir / 'all_keys.json'
    keys = json.loads(key_file.read_text('utf-8')) if key_file.exists() else {}
    db_dir = cfg.get('wechat_files_path', '')
    if not Path(db_dir).is_dir():
        raise ValueError('gh_ui 配置中的微信数据目录不存在。')
    if not keys and cfg.get('db_passphrase'):
        keys = keys_from_passphrase(bytes.fromhex(cfg['db_passphrase']), db_dir)
    if not keys:
        raise ValueError('所选 gh_ui 配置尚未保存可用密钥。')
    atomic_json(local_dir / 'wechat.json', {'db_dir': db_dir, 'keys': keys})
    _protect_credentials(local_dir / 'wechat.json')
    return db_dir


def _initialize_windows(db_dir, log):
    """Use the same active-first/fallback order as gh_ui api/wechat.py."""
    from wechat_native.scanner_win import extract_keys_active, extract_keys
    from .error_messages import redact

    def safe_log(message):
        log(redact(message))

    try:
        return extract_keys_active(str(db_dir), on_log=safe_log)
    except RuntimeError as exc:
        # gh_ui never kills a running client or falls back in this case.
        if '检测到微信正在运行' in str(exc):
            raise
        safe_log(f'[fallback] active 路径失败: {exc}, 改用上游 raw 内存扫描')
        try:
            return extract_keys(str(db_dir), on_log=safe_log)
        except RuntimeError as fallback:
            raise RuntimeError(f'WIN_ACTIVE_FAILED: {exc}\n[fallback] {fallback}') from fallback


def initialize(db_dir, local_dir, log=lambda s: None, *, capture=False):
    if not db_dir or not Path(db_dir).is_dir():
        raise ValueError('请先选择微信账号的数据目录。')
    if sys.platform == 'darwin':
        from wechat_native.scanner_mac import extract_keys
    elif os.name != 'nt':
        raise ValueError('微信读取目前支持 macOS 和 Windows。')
    # 上游日志可能含候选密钥，仅显示阶段，不转发内容到运营日志。
    log('正在调用 gh_ui 微信读取初始化，系统可能显示管理员授权窗口。')
    if sys.platform == 'darwin':
        if capture:
            from wechat_native.scanner_mac import extract_db_keys_lldb
            from wechat_native.key_scan import keys_from_enc_candidates
            log('请在微信中打开目标群、滚动聊天，触发数据库读取（约 40 秒）。')
            passphrase, candidates = extract_db_keys_lldb(timeout=40)
            keys = keys_from_passphrase(bytes.fromhex(passphrase), db_dir) if passphrase else {}
            if candidates:
                keys.update(keys_from_enc_candidates(candidates, db_dir))
        else:
            try:
                from wechat_native.scanner_mac import extract_keys
                keys = extract_keys(db_dir)
            except RuntimeError as exc:
                if '-2740' in str(exc):
                    raise ValueError('当前运行环境无法调用 macOS 系统授权（AppleScript -2740）。请从 Finder 双击本地工具后重试初始化。') from exc
                raise
    else:
        log('Windows 一次性初始化：请先从托盘完全退出微信，再在上游打开的微信窗口中登录同一账号。')
        keys = _initialize_windows(db_dir, log)
    if not keys:
        raise ValueError('现有扫描未取得密钥。请查看“连接微信”里的初始化说明；本次未修改目标 Excel。')
    config_path = Path(local_dir) / 'wechat.json'
    if config_path.exists():
        old = json.loads(config_path.read_text('utf-8'))
        if old.get('db_dir') == str(db_dir):
            keys = {**old.get('keys', {}), **keys}
    atomic_json(config_path, {'db_dir': str(db_dir), 'keys': keys})
    _protect_credentials(config_path)
    log(f'已保存 {len(keys)} 个数据库的读取凭据。')
    return len(keys)


def _protect_credentials(path):
    """Best-effort owner-only mode; Windows may ignore Unix permission bits."""
    try:
        Path(path).chmod(0o600)
    except OSError:
        pass


def repair_mac_permission():
    from wechat_native.scanner_mac import resign_wechat
    result = resign_wechat()
    if result.get('status') != 'ok':
        raise ValueError(f"{result.get('code', 'PERMISSION_REPAIR')}: {result.get('message', str(result))}")
    return result


class WeChatSource:
    def __init__(self, local_dir=None, log=lambda s: None):
        self.local_dir = Path(local_dir or data_home())
        self.log = log

    def config(self):
        path = self.local_dir / 'wechat.json'
        if not path.exists() and os.name != 'nt':
            for root in existing_configs():
                if (root / 'config.json').exists():
                    try:
                        import_gh_config(root, self.local_dir)
                        break
                    except (ValueError, OSError):
                        pass
        if not path.exists():
            raise ValueError('尚未连接微信。请点击“连接微信”，选择账号目录并初始化读取。')
        return json.loads(path.read_text('utf-8'))

    def refresh(self):
        cfg = self.config()
        db_dir = Path(cfg['db_dir'])
        if not db_dir.is_dir():
            raise ValueError('微信数据目录不存在，请重新连接微信。')
        keys = cfg.get('keys', {})
        required = []
        for folder in ('message', 'contact', 'session'):
            for p in (db_dir / folder).glob('*.db'):
                if folder == 'message' and not re.fullmatch(r'message_\d+\.db', p.name):
                    continue
                # 上游读取群名和消息不使用微信的 FTS 索引库；其专用
                # tokenizer 也无法由系统 SQLite 的 quick_check 加载。
                if folder == 'contact' and p.name != 'contact.db':
                    continue
                if folder == 'session' and p.name != 'session.db':
                    continue
                required.append(p)
        if not any(re.fullmatch(r'message_\d+\.db', p.name) for p in required):
            raise ValueError('该账号目录中没有聊天消息数据库。')
        # 上游会跳过失败的库；业务更新必须在读全后才推进进度。
        for path in required:
            with path.open('rb') as stream:
                page = stream.read(4096)
            salt = page[:16].hex()
            value = keys.get(salt)
            if not value or not verify_enc_key(bytes.fromhex(value), page):
                raise ValueError(f'读取凭据未覆盖或无法解密 {path.parent.name}/{path.name}，请重新初始化；本次不会推进更新进度。')
        cache = self.local_dir / 'decrypted'
        warnings = []
        def record(level, text):
            if level in ('warning', 'error'):
                warnings.append(str(text))
        mapping = native.decrypt_all_dbs(str(db_dir), keys, str(cache), on_log=record)
        if any('wal apply failed' in text for text in warnings):
            for path in mapping.values():
                Path(path + '.meta').unlink(missing_ok=True)
            raise ValueError('微信最新消息日志合并失败，本次停止更新，请重试。')
        for path in required:
            relative = str(path.relative_to(db_dir))
            if relative not in mapping:
                raise ValueError(f'数据库 {relative} 未成功刷新，本次停止更新。')
            # 读全库确保缓存不是损坏的 SQLite，不把查询失败误报为零条消息。
            with sqlite3.connect(mapping[relative]) as conn:
                check = conn.execute('PRAGMA quick_check').fetchone()
                if not check or check[0] != 'ok':
                    raise ValueError(f'数据库 {relative} 完整性检查失败。')
        self.log(f'微信数据刷新成功，已核对 {len(required)} 个核心数据库。')
        return cache

    def fetch(self, start, end, group):
        cache = self.refresh()
        sessions = native.list_sessions(str(cache))
        matches = [s for s in sessions if s['display_name'] == group]
        if len(matches) != 1:
            raise ValueError(f'精确匹配群“{group}”得到 {len(matches)} 个会话；请核对账号及群名。')
        session = matches[0]
        identifier = session['username'] or session['chat_name']
        # 原查询的时间按系统本地时区解析，先把北京时间转成运行电脑本地时间。
        start_local = start.astimezone().strftime('%Y-%m-%d %H:%M:%S')
        end_local = end.astimezone().strftime('%Y-%m-%d %H:%M:%S')
        result = native.search_messages(str(cache), start_date=start_local, end_date=end_local,
                                        chat_name=identifier, keyword='#接龙', limit=0, full_content=True,
                                        sender_from_raw_prefix=True)
        for item in result:
            item['time'] = datetime.fromtimestamp(item['create_ts'], BJT).strftime('%Y-%m-%d %H:%M:%S')
        self.log(f'已定位“{group}”，读取 {len(result)} 条完整接龙。')
        return result

    def inspect(self, group='GH派点反馈群'):
        cache = self.refresh()
        sessions = native.list_sessions(str(cache))
        matches = [s for s in sessions if s['display_name'] == group]
        if len(matches) != 1:
            raise ValueError(f'精确匹配群“{group}”得到 {len(matches)} 个会话；请核对账号及群名。')
        return {'group': group, 'matched_sessions': matches, 'session_count': len(sessions)}
