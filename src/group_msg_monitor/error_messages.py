"""本工具的故障说明与本地记录；不参与微信连接或采集。"""
from __future__ import annotations

import json
import os
import re
import sys
import traceback
from pathlib import Path

from .relay_core import now_bjt
from .paths import resource_root


def guide_path():
    return resource_root() / 'data/diagnostics/error_message.md'


def diagnostics_home(local_dir):
    local = Path(local_dir)
    return (local.parent if local.name == 'local_data' else local) / 'diagnostics'


def append_private_text(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(fd, 'a', encoding='utf-8') as stream:
        if hasattr(os, 'fchmod'):
            os.fchmod(stream.fileno(), 0o600)
        stream.write(text)


def record_runtime_log(local_dir, line):
    try:
        append_private_text(diagnostics_home(local_dir) / 'runtime.log', redact(line))
    except OSError:
        pass  # 排错材料不可写也不能中断更新。


def redact(text):
    # 上游异常可能包含候选密钥，记录前移除长十六进制串。
    return re.sub(r'(?i)[0-9a-f]{32,}', '[已隐藏密钥或标识]', str(text))


def error_message(exc):
    text = str(exc)
    lower = text.lower()
    if '-2740' in text or '/bin/ps' in text and 'operation not permitted' in lower:
        return ('EXECUTION_CONTEXT', '当前启动环境无法执行系统授权或进程检查。',
                '从 Finder 双击最新版应用再试。保留原始错误；Codex 的 Full access 与 macOS 系统授权是两回事。')
    if 'APP_PERMISSION_DENIED' in text or 'macOS 拒绝修改' in text:
        return ('APP_PERMISSION', 'macOS 拒绝修复微信应用权限。',
                '先正常退出微信；检查“隐私与安全性 → App 管理”和“完全磁盘访问权限”。若仍失败，按排错指南检查日志中的权限归属，不要仅凭此错误认定是 App Store 版或卸载微信。')
    if 'task_for_pid' in text or 'RESIGN_NEEDED' in text or 'kr=5' in text:
        return ('WECHAT_PROCESS_ACCESS', '尚未取得微信进程的读取权限。',
                '先正常退出微信，再在“连接微信”中执行“修复 Mac 读取权限”，重新打开并登录后初始化。此修复只在需要时执行。')
    if 'WECHAT_RUNNING' in text:
        return ('WECHAT_RUNNING', '修复读取权限时微信仍在运行。',
                '通过微信菜单正常退出整个应用，再重试修复；日常读取可以一直保持微信登录。')
    if 'operation not permitted' in lower or '读取权限' in text and '授权' in text:
        return ('SYSTEM_FILE_ACCESS', '系统拒绝访问所需文件。',
                '检查“隐私与安全性 → 完全磁盘访问权限”。要按日志确认实际被拒绝的程序；本次曾归属到 authtrampoline，而不是已授权的另一个 Python。具体路径和步骤见排错指南。')
    if '_mac_helper.py' in text and ('no such file' in lower or '缺失' in text):
        return ('HELPER_PATH', '微信初始化辅助脚本未能启动。',
                '先核对辅助脚本是否存在；文件存在却报找不到时，检查错误路径是否带字面双引号。本次已修复参数重复引用，请使用最新版应用，不要盲目补文件。')
    if 'NO_WECHAT' in text or '微信未运行' in text:
        return ('WECHAT_NOT_RUNNING', '没有检测到运行中的微信。',
                '打开并登录目标账号，确认能看到目标群，再点“测试群连接”。已有凭据时无需再次抓取。')
    if os.name == 'nt' and ('access is denied' in lower or '拒绝访问' in text or '5' == text.strip() or 'winerror 5' in lower):
        return ('WINDOWS_ADMIN', 'Windows 拒绝读取微信进程内存。',
                '以管理员身份运行 start.bat（或管理员 PowerShell 启动本工具），保持微信已登录后，在“微信连接设置 → 高级维护”执行普通初始化。不要用 Mac 的签名修复步骤。')
    if os.name == 'nt' and ('access is denied' in lower or '拒绝访问' in text or '5' == text.strip() or 'winerror 5' in lower):
        return ('WINDOWS_ADMIN', 'Windows 拒绝读取微信进程内存。',
                '以管理员身份运行 start.bat（或管理员 PowerShell 启动本工具），保持微信已登录后，在“微信连接设置 → 高级维护”执行普通初始化。不要用 Mac 的签名修复步骤。')
    if 'NO_LLDB' in text:
        return ('DEBUGGER_MISSING', '新版初始化所需的系统调试工具未安装。',
                '按原版错误提示安装 Xcode Command Line Tools；已有有效凭据的日常更新不使用调试器。')
    if 'NO_SYMBOL' in text:
        return ('CAPTURE_SYMBOLS', '新版初始化没有定位到原版所需的加密函数。',
                '记录微信版本及完整错误，核对 gh_ui 对此版本的支持。不要把它解释成群没有数据，也不要自行改成其他采集方式。')
    if 'CAPTURE_FAILED' in text or '现有扫描未取得密钥' in text:
        return ('KEY_CAPTURE', '本次初始化没有取得有效密钥。',
                '先重新检查微信状态；若显示已就绪，直接开始更新，不需要初始化。只有首次使用或确实缺少凭据时，才从高级维护进行一次性初始化（先普通、再新版）。不要为日常更新反复抓取。')
    if '尚未连接微信' in text or '凭据未覆盖' in text or '无法解密' in text or '未保存可用密钥' in text:
        return ('KEY_COVERAGE', '读取凭据缺失或未覆盖必需数据库。',
                '先核对当前账号和数据目录，以及报错中具体缺少的库。需要时只补充一次初始化，已有密钥会累积保留；不要删除 local_data 后盲目重试。')
    if '数据目录不存在' in text or '没有聊天消息数据库' in text:
        return ('ACCOUNT_PATH', '账号数据目录不存在或不含消息库。',
                '打开“微信连接设置”，点击“重新识别”；未找到时点“选择文件夹”，选择微信设置中的文件存储位置、账号目录或 db_storage。多个候选时核对当前账号；微信号与磁盘 wxid 名称可能不同，不能靠排序猜选。选择路径不会自动初始化。')
    if '精确匹配群' in text:
        return ('GROUP_MATCH', '没有得到唯一的目标群会话。',
                '核对当前登录账号及完整群名，再测试群连接。若通讯录库未成功解密，群名无法还原；0 个匹配不代表群里没有消息。')
    if 'sql logic error' in lower:
        return ('SQLITE_INDEX', '本地数据库检查遇到 SQLite 查询错误。',
                '查看故障记录中的库路径和调用位置。本次曾因误检查微信专用 FTS 索引库失败，最新版已只校验实际读取的核心库；不要据此删除微信数据库。')
    if type(exc).__name__ == 'ParseError' and type(exc).__module__ == 'xml.etree.ElementTree':
        return ('WORKBOOK_XML', '目标工作簿 XML 解析失败。',
                '保留更新前文件和故障记录，使用最新版后重试。本次曾修复连续空单元格被错误合并解析的问题；若仍失败，不要覆盖或重建原始工作簿。')
    if '500 字' in text:
        return ('CONTENT_TRUNCATED', '接龙正文读取不完整。',
                '最新版已支持完整正文，请关闭旧窗口后用最新版重新点击“开始更新”。这不是密钥问题，不要重新初始化微信；若仍出现，请保留运行日志反馈。')
    if '日志合并失败' in text or '未成功刷新' in text or '完整性检查失败' in text:
        return ('DATABASE_REFRESH', '最新消息数据库未完整刷新。',
                '保持微信正常登录后重试一次；持续失败时保留故障记录和微信版本，检查 WAL 合并及具体数据库错误。不要删除原始微信数据库。')
    if '正在使用此文件' in text or '被其他程序修改' in text:
        return ('EXCEL_IN_USE', '目标 Excel 正在使用或已被其他程序修改。',
                '保存并关闭 Excel/WPS 里的目标文件，再重新运行。')
    if '正在更新' in text or '.relay.lock' in text:
        return ('UPDATE_LOCK', '目标文件已有更新任务或遗留锁。',
                '先确认其他更新任务已结束；仅在确认没有工具进程更新此文件后，处理提示中的 .relay.lock。')
    if '进度文件损坏' in text:
        return ('STATE_FILE', '更新进度文件无法读取。',
                '保留当前文件并从归档恢复正确进度，核对时间后再运行；不要直接删除进度跳过消息。')
    if isinstance(exc, (ImportError, ModuleNotFoundError)):
        return ('PACKAGE_DEPENDENCY', '应用缺少运行依赖。',
                '使用包含依赖的最新版完整应用包；反馈故障记录中的缺失模块名，不要只复制包内的可执行文件。')
    return ('UNCLASSIFIED', '操作未完成，需要查看具体错误。',
            '先保留故障记录，按“排错指南”核对出错阶段，再反馈错误编号、时间和原始错误。')


def record_error(local_dir, exc, operation):
    code, summary, action = error_message(exc)
    report = {
        'time_bjt': now_bjt().isoformat(), 'operation': operation,
        'code': code, 'summary': summary, 'action': action,
        'exception_type': type(exc).__name__, 'error': redact(exc),
        'traceback': redact(''.join(traceback.format_exception(type(exc), exc, exc.__traceback__))),
    }
    path = Path(local_dir) / 'error_message.jsonl'
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, 'a', encoding='utf-8') as stream:
            if hasattr(os, 'fchmod'):
                os.fchmod(stream.fileno(), 0o600)
            else:
                path.chmod(0o600)
            stream.write(json.dumps(report, ensure_ascii=False) + '\n')
        report['record_path'] = str(path)
    except OSError as log_error:
        report['record_error'] = redact(log_error)
    try:
        mirror = diagnostics_home(local_dir) / 'error_message.jsonl'
        append_private_text(mirror, json.dumps(report, ensure_ascii=False) + '\n')
        report['diagnostic_path'] = str(mirror)
    except OSError:
        pass
    return report
