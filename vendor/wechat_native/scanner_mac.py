"""macOS 微信进程内存扫描 + 重签名 (用于自动获取数据库密钥).

职责:
  本模块仅在 macOS 上有意义. 提供两个独立入口:
    1. resign_wechat()   -- 给 WeChat.app 重签名, 追加 get-task-allow entitlement,
                           使非 App Store 版本也能被 task_for_pid 访问内存.
    2. extract_keys()    -- 通过 _mac_helper.py 扫描微信进程内存, 提取 SQLCipher 密钥.
    3. extract_image_keys() -- 扫描图片 AES 密钥 (用于 .dat 文件解密).

整体流程 (用户视角):
  1. 用户点击「重签名微信」按钮:
     - osascript 弹系统密码框 (sudo 等价)
     - 在 root 权限下: codesign --force --sign - --entitlements 给 WeChat.app
       追加 com.apple.security.get-task-allow entitlement
     - 保留微信原有 entitlements
  2. 用户手动退出 WeChat -> 重新打开 (重签后必须重启进程才生效)
  3. 用户点击「自动获取密码」:
     - osascript 再次弹密码框
     - 在 root 权限下用 /usr/bin/python3 跑 _mac_helper.py
     - helper 用 ctypes 调 task_for_pid + mach_vm_read 扫内存
     - 输出 {salt_hex: enc_key_hex} JSON

平台差异:
  * macOS 进程间内存访问须经 Mach task port. 非调试版 App 默认拒绝 task_for_pid,
    因此必须先重签名追加 com.apple.security.get-task-allow entitlement.
  * Windows 对应实现见 scanner_win.py, 通过 ReadProcessMemory 直读, 无需重签名.

被谁调用:
  * api/wechat.py 的 FastAPI 路由 /api/wechat/resign 和 /api/wechat/extract_keys
  * 测试脚本和 CLI 可直接 from wechat_native.scanner_mac import resign_wechat, extract_keys

权衡:
  * 重签名微信会让微信下次自动更新失效 (App Store 不会更新签名修改过的版本),
    用户需要重新跑「重签名」流程
  * 弹密码框由 osascript 完成, 不需要给 sidecar 申请额外 entitlement
"""

from __future__ import annotations

import json
import os
import platform
import plistlib
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable

IS_MACOS = platform.system() == "Darwin"

# 微信数据库在 macOS 沙盒容器下的几种可能路径, 按优先级排序.
# 应用沙盒容器路径 (推荐) > 应用容器根目录 > 旧版非沙盒路径.
CANDIDATE_DB_ROOTS = [
    "Library/Containers/com.tencent.xinWeChat/Data/Library/Application Support/com.tencent.xinWeChat",
    "Library/Containers/com.tencent.xinWeChat",
    "Library/Application Support/com.tencent.xinWeChat",
]


def detect_db_storage(home: str | None = None) -> str:
    """定位微信本地数据库目录 (db_storage).

    依次遍历 CANDIDATE_DB_ROOTS 中的候选路径, 在每个候选根目录下递归搜索
    名为 db_storage 的子目录. 若找不到 db_storage 则退化到候选根目录本身.

    参数:
        home: 用户家目录, 默认取 os.path.expanduser("~").

    返回:
        找到的目录绝对路径字符串; 未找到时返回空字符串.
    """
    base = Path(home or os.path.expanduser("~"))
    for rel in CANDIDATE_DB_ROOTS:
        root = base / rel
        if not root.exists():
            continue
        # 优先寻找 db_storage 子目录 (微信 3.x/4.x 均有)
        for child in root.rglob("db_storage"):
            if child.is_dir():
                return str(child)
        # 退化: 返回候选根本身
        if root.is_dir():
            return str(root)
    return ""


# ─── 微信应用定位 ──────────────────────────────
# 微信在 macOS 上可能有两个 Bundle ID: xinWeChat (Mac 原生版) 和 WeChat (Catalyst/旧版).
WECHAT_BUNDLE_IDS = ("com.tencent.xinWeChat", "com.tencent.WeChat")
WECHAT_APP_NAME_RE = re.compile(r"^(wechat|weixin|微信)(\s+\d+)?\.app$", re.IGNORECASE)


def _bundle_id(app_path: Path) -> str:
    """内部辅助函数: 处理  bundle id 中间步骤，供微信本地数据流程复用。"""
    info = app_path / "Contents" / "Info.plist"
    try:
        with info.open("rb") as f:
            data = plistlib.load(f)
    except Exception:
        return ""
    return str(data.get("CFBundleIdentifier") or "")


def _scan_application_root(root: Path) -> str:
    """Find a WeChat bundle in one Applications directory."""
    try:
        if not root.is_dir():
            return ""
        apps = sorted(root.iterdir(), key=lambda p: p.name.lower())
    except OSError:
        return ""

    # Bundle ID is authoritative and handles renamed or duplicated app bundles.
    for app in apps:
        if app.suffix.lower() == ".app" and _bundle_id(app) in WECHAT_BUNDLE_IDS:
            return str(app)

    # Fallback for damaged or incomplete metadata, still constrained to app names.
    for app in apps:
        if app.suffix.lower() == ".app" and WECHAT_APP_NAME_RE.fullmatch(app.name):
            return str(app)
    return ""


def find_wechat_app() -> str:
    """定位微信应用安装路径.

    策略:
      1. 优先检查系统级和用户级 /Applications 固定路径, 避免 Spotlight 索引延迟.
      2. 兜底用 mdfind 按 Bundle ID 全盘搜索 (需 Spotlight 已索引).

    返回:
        微信应用目录的绝对路径; 未找到时返回空字符串.
    """
    # 固定路径优先 (最常见的安装位置; 国区安装名常是“微信.app”)
    fixed_paths = (
        "/Applications/WeChat.app",
        "/Applications/微信.app",
        "/Applications/Weixin.app",
        os.path.expanduser("~/Applications/WeChat.app"),
        os.path.expanduser("~/Applications/微信.app"),
        os.path.expanduser("~/Applications/Weixin.app"),
    )
    for p in fixed_paths:
        if os.path.isdir(p):
            return p
    # 扫描 Applications 目录, 覆盖用户改名/重复安装后的任意 .app 路径.
    for root in (Path("/Applications"), Path.home() / "Applications"):
        found = _scan_application_root(root)
        if found:
            return found
    # Spotlight 搜索 (慢但能覆盖非标准安装位置)
    for bid in WECHAT_BUNDLE_IDS:
        try:
            out = subprocess.run(
                ["mdfind", f"kMDItemCFBundleIdentifier == '{bid}'"],
                capture_output=True, text=True, timeout=10,
            )
        except Exception:
            continue
        for line in out.stdout.splitlines():
            line = line.strip()
            if line and os.path.isdir(line):
                return line
    return ""


# ─── 重签名 ──────────────────────────────────────
def _extract_entitlements(app_path: str) -> dict:
    """读取微信应用当前的 entitlements (不需要 sudo).

    使用 codesign -d --entitlements :- 输出原始 plist 二进制到 stdout,
    再由 plistlib.loads 解析. 若应用未签名或命令失败, 返回空字典.

    参数:
        app_path: 微信应用目录路径.

    返回:
        entitlements 字典; 解析失败时返回 {}.
    """
    try:
        out = subprocess.run(
            ["codesign", "-d", "--entitlements", ":-", app_path],
            capture_output=True, timeout=15,
        )
    except Exception:
        return {}
    if out.returncode != 0 or not out.stdout:
        return {}
    try:
        return plistlib.loads(out.stdout) or {}
    except Exception:
        return {}


def _build_entitlements(app_path: str) -> bytes:
    """在现有 entitlements 基础上追加 get-task-allow, 序列化为 XML plist.

    get-task-allow = True 允许其他进程通过 task_for_pid 获取该进程的 task port,
    是 macOS 内存扫描的前提条件.

    参数:
        app_path: 微信应用目录路径.

    返回:
        包含新 entitlements 的 XML plist 字节串, 可直接写入临时文件供 codesign 使用.
    """
    ent = _extract_entitlements(app_path)
    ent["com.apple.security.get-task-allow"] = True
    return plistlib.dumps(ent, fmt=plistlib.FMT_XML)


def _applescript_string(value: str) -> str:
    """Quote a Python string as an AppleScript string literal."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _osascript_run_with_admin(shell_cmd: str, timeout: int = 180) -> tuple[int, str, str]:
    """通过 osascript 弹原生密码框, 用 admin 权限跑 shell_cmd.

    shell_cmd 中如果有双引号需要 escape; 这里假设调用方已经处理.
    返回 (returncode, stdout, stderr).
    用户取消授权时 osascript exit != 0, stderr 含 "User canceled".
    """
    script = f"do shell script {_applescript_string(shell_cmd)} with administrator privileges"
    try:
        proc = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return 124, "", "osascript timeout"
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _has_get_task_allow(app_path: str) -> bool:
    """Return True when the WeChat bundle already carries get-task-allow."""
    return bool(_extract_entitlements(app_path).get("com.apple.security.get-task-allow"))


def _list_wechat_processes_for_app(app_path: str) -> list[tuple[int, str]]:
    """List running processes whose command path belongs to the target app bundle."""
    try:
        app_real = str(Path(app_path).resolve())
    except OSError:
        app_real = app_path
    proc = subprocess.run(
        ["/bin/ps", "-axo", "pid=,command="],
        capture_output=True, text=True, timeout=10,
    )
    matches: list[tuple[int, str]] = []
    for raw in proc.stdout.splitlines():
        line = raw.strip()
        if not line:
            continue
        pid_s, _, cmd = line.partition(" ")
        if not pid_s.isdigit():
            continue
        if cmd.startswith(app_real + "/") or cmd.startswith(app_path + "/"):
            matches.append((int(pid_s), cmd))
    return matches


def _is_mac_app_store_build(app_path: str) -> bool:
    """检测是否为 Mac App Store 安装的版本。

    App Store 版 app 在 `Contents/_MASReceipt/receipt` 留有 Apple 签发的购买收据;
    这类 app 受系统保护, `codesign` 重签名必然失败 (Operation not permitted) 且无法绕过。
    提前识别后应引导用户改用官网版微信, 而不是让其反复尝试注定失败的授权弹框。
    """
    return os.path.isfile(os.path.join(app_path, "Contents", "_MASReceipt", "receipt"))


def _codesign_permission_message(app_path: str) -> str:
    """内部辅助函数: 处理  codesign permission message 中间步骤，供微信本地数据流程复用。"""
    return (
        f"macOS 拒绝修改 {app_path} (Operation not permitted)。\n"
        "请按顺序处理后重试:\n"
        "1. 完全退出微信: 菜单栏/ Dock 右键退出, 确认没有 WeChat/微信/WeChatAppEx/Updater 进程。\n"
        "2. 打开 系统设置 -> 隐私与安全性 -> App 管理, 允许「国海金工量化终端」。\n"
        "3. 同一页的 完全磁盘访问权限 中也允许「国海金工量化终端」。\n"
        "4. 若仍失败, 多半是 App Store 版微信(受系统保护无法重签名): 请到微信官网 "
        "https://mac.weixin.qq.com 下载安装非 App Store 版微信后重试。"
    )


def _classify_codesign_failure(app_path: str, text: str) -> tuple[str, str]:
    """Map raw codesign/osascript output to a stable UI error code and message."""
    low = text.lower()
    if "operation not permitted" in low:
        return "APP_PERMISSION_DENIED", _codesign_permission_message(app_path)
    if "text file busy" in low or "resource busy" in low:
        return (
            "WECHAT_RUNNING",
            "微信仍在运行, macOS 正在占用 App bundle。请完全退出微信和更新器后再重签名。",
        )
    return "CODESIGN_FAILED", text.strip() or "codesign 失败"


def resign_wechat() -> dict:
    """对微信应用重签名, 追加 com.apple.security.get-task-allow entitlement.

    重签名是 macOS 内存扫描的前置步骤: 只有携带该 entitlement 的进程, 其他进程才能
    通过 task_for_pid 获取 Mach task port 并读取内存.

    副作用:
      * 微信应用的代码签名被替换为临时 ad-hoc 签名 (-), App Store 版本将不再
        自动更新 (需用户手动重签).
      * 操作需要管理员密码, 通过 osascript 弹原生密码框获取.

    返回:
        字典结构: {"status": "ok"|"error", "code": str, "message": str, "app_path": str}
        可能的 error code:
          PLATFORM      -- 非 macOS 平台
          NO_WECHAT_APP -- 未找到微信应用
          OTHER         -- entitlements 提取/写入失败
          USER_CANCELLED -- 用户取消系统密码弹框
          CODESIGN_FAILED -- codesign 命令返回非零
          APP_STORE_WECHAT -- App Store 版微信, 受保护无法重签名 (需改用官网版)
          APP_PERMISSION_DENIED -- 系统拒绝修改 (App 管理/完全磁盘访问 未授权)
          WECHAT_RUNNING -- 微信仍在运行, 占用 App bundle
    """
    if not IS_MACOS:
        return {"status": "error", "code": "PLATFORM", "message": "仅 macOS 可用"}

    app = find_wechat_app()
    if not app:
        return {
            "status": "error",
            "code": "NO_WECHAT_APP",
            "message": "未找到微信应用, 请先安装并登录微信",
        }

    already_signed = _has_get_task_allow(app)
    running = _list_wechat_processes_for_app(app)
    if already_signed:
        next_step = (
            "请完全退出微信后重新打开, 再点「自动抓密钥」。"
            if running else
            "请打开并登录微信, 再点「自动抓密钥」。"
        )
        return {
            "status": "ok",
            "code": "OK",
            "message": f"微信已具备 get-task-allow entitlement, 无需重复重签名。{next_step}",
            "app_path": app,
        }
    # App Store 版微信受系统保护, 重签名必然失败 (Operation not permitted), 提前识别并引导
    # 用户改用官网版, 省去一次注定失败的密码授权弹框 —— 这是该错误最常见的真实根因。
    if _is_mac_app_store_build(app):
        return {
            "status": "error",
            "code": "APP_STORE_WECHAT",
            "message": (
                "检测到当前是 App Store 版微信, 受系统保护无法重签名"
                "(这正是「Operation not permitted」的根因)。请改用官网版微信:\n"
                "1. 完全退出并卸载 App Store 版微信;\n"
                "2. 到微信官网 https://mac.weixin.qq.com 下载安装 Mac 版微信;\n"
                "3. 登录后重新点「重签名微信」即可。"
            ),
            "app_path": app,
        }
    if running:
        pids = ", ".join(str(pid) for pid, _cmd in running[:8])
        return {
            "status": "error",
            "code": "WECHAT_RUNNING",
            "message": (
                f"检测到微信仍在运行 (PID: {pids})。请先完全退出微信 "
                "(菜单栏/ Dock 右键退出, 不只是关闭窗口), 再点击「重签名微信」。"
            ),
            "app_path": app,
        }

    # 写入 entitlements 到 /tmp/ (普通权限即可, sudo 时还能读)
    try:
        ent_xml = _build_entitlements(app)
    except Exception as e:
        return {"status": "error", "code": "OTHER", "message": f"entitlements 提取失败: {e}"}
    ent_path = tempfile.mkstemp(suffix=".plist", prefix="wechat_ent_")[1]
    try:
        with open(ent_path, "wb") as f:
            f.write(ent_xml)
        # 确保 root 进程也可读 (osascript 切换到 admin 后 umask 可能限制访问)
        os.chmod(ent_path, 0o644)

        # 重签名 (sudo 跑). xattr/chflags 清理是 best-effort, 用于处理下载隔离和手动复制
        # App 时留下的不可写标记; 真正失败仍以 codesign 返回为准。
        app_q = shlex.quote(app)
        ent_q = shlex.quote(ent_path)
        cmd = (
            f"/usr/bin/xattr -dr com.apple.quarantine {app_q} 2>/dev/null || true; "
            f"/usr/bin/chflags -R nouchg,noschg {app_q} 2>/dev/null || true; "
            f"/usr/bin/codesign --force --sign - --entitlements {ent_q} {app_q}"
        )
        rc, out, err = _osascript_run_with_admin(cmd, timeout=120)
        if rc != 0:
            if "canceled" in err.lower() or "user canceled" in err.lower():
                return {"status": "error", "code": "USER_CANCELLED", "message": "用户取消授权"}
            code, message = _classify_codesign_failure(app, err or out)
            return {
                "status": "error",
                "code": code,
                "message": message,
                "app_path": app,
            }
    finally:
        # 无论成败均删除临时 plist, 避免密钥相关内容残留在 /tmp/
        try:
            os.remove(ent_path)
        except OSError:
            pass

    return {
        "status": "ok",
        "code": "OK",
        "message": "重签名完成。请退出微信(完全退出, 不只是最小化)并重新打开, 然后再点「自动获取密码」。",
        "app_path": app,
    }


# ─── 内存扫描 (调 _mac_helper.py) ─────────────────
def _helper_path() -> Path:
    """定位 _mac_helper.py 的绝对路径.

    PyInstaller 打包后, 同包文件会被解压到 sys._MEIPASS/wechat_native/ 目录;
    开发模式下 _mac_helper.py 与本文件在同一目录. 两种情况均用 __file__ 推导.

    返回:
        _mac_helper.py 的 Path 对象 (不保证存在, 调用方需检查).
    """
    here = Path(__file__).resolve().parent
    return here / "_mac_helper.py"


def extract_keys(db_dir: str, on_log: Callable[[str], None] | None = None) -> dict[str, str]:
    """通过 osascript+sudo 运行 _mac_helper.py 扫描微信内存, 提取 SQLCipher 加密密钥.

    内部流程:
      1. 调用 _osascript_run_with_admin 弹密码框, 以管理员身份运行 helper.
      2. helper (以 root 权限运行) 调用 task_for_pid 获取微信进程 Mach task port,
         通过 mach_vm_read 读取所有可读内存区域, 扫描 x'<64hex>' 模式.
      3. 候选 enc_key 经 HMAC-SHA512 与 db 文件的 page1 比对验证.
      4. helper 将结果以 JSON 输出到 stdout, 本函数解析并返回.

    参数:
        db_dir:  微信数据库目录 (即 detect_db_storage 的返回值).
        on_log:  可选日志回调, 签名为 (msg: str) -> None.

    返回:
        {salt_hex: enc_key_hex} 字典, 每个 enc_key 均已通过 HMAC 验证.

    异常:
        RuntimeError: 用户取消授权 / task_for_pid 失败 / 微信未运行 / helper 缺失 / JSON 解析失败.
                      错误消息前缀 (USER_CANCELLED / RESIGN_NEEDED / NO_WECHAT) 供上层路由识别.
    """
    if not IS_MACOS:
        raise RuntimeError("scanner_mac 仅 macOS 可用")
    log = on_log or (lambda *_a, **_k: None)

    helper = _helper_path()
    if not helper.exists():
        raise RuntimeError(f"helper script 缺失: {helper}")

    # shell 参数在此引用一次，AppleScript 字符串转义由授权函数统一处理。
    cmd = shlex.join([
        "/usr/bin/python3", str(helper), "--db-dir", str(db_dir), "--limit-mb", "600",
    ])
    log(f"osascript run: {cmd}")
    rc, out, err = _osascript_run_with_admin(cmd, timeout=300)

    if rc != 0:
        e = err.lower()
        if "canceled" in e or "user canceled" in e:
            raise RuntimeError("USER_CANCELLED: 用户取消授权")
        # task_for_pid 失败说明微信未完成重签名, 提示用户先走 resign 流程
        if "task_for_pid" in (out + err):
            raise RuntimeError(
                "RESIGN_NEEDED: task_for_pid 失败。请先点「重签名微信」, 重启微信后再试。"
            )
        raise RuntimeError((err or out or "scan helper failed").strip())

    # 解析 helper 的 JSON 输出 (格式: {"keys": {...}, "log": [...], "error": null})
    try:
        data = json.loads(out.strip())
    except json.JSONDecodeError as e:
        raise RuntimeError(f"helper 输出非 JSON: {e}; out[:200]={out[:200]!r}")

    # 将 helper 内部日志转发给调用方
    for line in data.get("log") or []:
        log(line)

    if data.get("error"):
        err_text = str(data["error"])
        # kr= 是 Mach 内核返回码前缀, 表示 task_for_pid 底层错误
        if "task_for_pid" in err_text or "kr=" in err_text:
            raise RuntimeError("RESIGN_NEEDED: task_for_pid 失败, 请先「重签名微信」并重启微信")
        if "not running" in err_text.lower():
            raise RuntimeError("NO_WECHAT: 微信未运行, 请先打开并登录微信")
        raise RuntimeError(err_text)

    keys = data.get("keys") or {}
    if not isinstance(keys, dict):
        raise RuntimeError("helper 返回 keys 字段类型错误")
    # 过滤: enc_key 必须是 64 字符合法 hex 串; lower() 统一大小写便于后续比对
    return {
        str(k): str(v).lower()
        for k, v in keys.items()
        if isinstance(k, str) and isinstance(v, str) and len(v) == 64
    }


def extract_image_keys(
    dat_dir: str,
    on_log: Callable[[str], None] | None = None,
) -> tuple[dict[str, str], list[str]]:
    """通过 osascript+sudo 运行 helper 扫描微信内存, 提取图片 AES 密钥.

    与 extract_keys (DB 模式) 共用同一个 _mac_helper.py, 区别在于传入
    --mode image-key 和 --dat-dir 参数. helper 的扫描策略:
      * 取 dat_dir 下前 N 个 .dat 文件的头 16 字节作为 AES 密文 pattern
      * 在微信进程堆内存中扫描 16 字节候选 key (通过已知 magic 做强校验)
      * 累积每个 key 的跨文件命中数; 命中 >=2 的视为权威密钥 (primary_keys)

    注意: image key 是堆里的 32-char ASCII 原文 (非 hex 编码), decrypt_v4 取其
    前 16 个 ASCII 字节作为 AES-128 key. 这里绝不能 lower() / 重新编码,
    否则字节会变导致解密失败.

    参数:
        dat_dir: 微信图片 .dat 文件所在目录.
        on_log:  可选日志回调.

    返回:
        (key_map, primary_keys) 二元组:
          - key_map:      {ct_hex: aes_key_str} 每个密文 pattern 对应的 key
          - primary_keys: 跨 pattern 通用的真实密钥候选列表 (按命中数倒序)

    异常:
        RuntimeError: 用户取消 / task_for_pid 失败 / 微信未运行 / dat 目录无效等.
                      错误前缀含义同 extract_keys.
    """
    if not IS_MACOS:
        raise RuntimeError("scanner_mac 仅 macOS 可用")
    log = on_log or (lambda *_a, **_k: None)

    helper = _helper_path()
    if not helper.exists():
        raise RuntimeError(f"helper script 缺失: {helper}")

    # timeout 600s: 图片 key 扫描比 DB key 扫描范围更大, 给予更充裕的时间
    cmd = (
        f"/usr/bin/python3 \\\"{helper}\\\" --mode image-key "
        f"--dat-dir \\\"{dat_dir}\\\" --limit-mb 600"
    )
    log(f"osascript run: {cmd}")
    rc, out, err = _osascript_run_with_admin(cmd, timeout=600)

    if rc != 0:
        e = err.lower()
        if "canceled" in e or "user canceled" in e:
            raise RuntimeError("USER_CANCELLED: 用户取消授权")
        if "task_for_pid" in (out + err):
            raise RuntimeError(
                "RESIGN_NEEDED: task_for_pid 失败。请先点「重签名微信」, 重启微信后再试。"
            )
        raise RuntimeError((err or out or "image-key helper failed").strip())

    try:
        data = json.loads(out.strip())
    except json.JSONDecodeError as e:
        raise RuntimeError(f"helper 输出非 JSON: {e}; out[:200]={out[:200]!r}")

    for line in data.get("log") or []:
        log(line)

    if data.get("error"):
        err_text = str(data["error"])
        if "task_for_pid" in err_text:
            raise RuntimeError("RESIGN_NEEDED: task_for_pid 失败, 请先「重签名微信」并重启微信")
        if "not running" in err_text.lower():
            raise RuntimeError("NO_WECHAT: 微信未运行, 请先打开并登录微信")
        raise RuntimeError(err_text)

    keys = data.get("keys") or {}
    if not isinstance(keys, dict):
        raise RuntimeError("helper 返回 keys 字段类型错误")
    # image key 是堆里的 32-char ASCII 原文, decrypt_v4 取其前 16 个 ASCII 字节当真实 key,
    # 这里绝不能 lower() / 重新编码 (会改变字节导致解不开)
    key_map = {
        str(k): str(v)
        for k, v in keys.items()
        if isinstance(k, str) and isinstance(v, str) and len(v) == 32
    }
    # primary_keys 是跨多个 .dat pattern 均命中的候选, 可信度最高
    primary = [
        str(k)
        for k in (data.get("primary_keys") or [])
        if isinstance(k, str) and len(k) == 32
    ]
    return key_map, primary


# ─── 数据库口令抓取 (微信 4.1.x, lldb 断点) ─────────
def _lldb_capture_path() -> Path:
    """_mac_lldb_capture.py 的绝对路径 (与本文件同目录; 打包时作为 data 一并解压)。"""
    return Path(__file__).resolve().parent / "_mac_lldb_capture.py"


def _find_lldb() -> str | None:
    """定位 lldb 可执行文件: 优先 xcrun -f lldb, 退回 /usr/bin/lldb。"""
    try:
        out = subprocess.run(["xcrun", "-f", "lldb"], capture_output=True, text=True, timeout=10)
        p = out.stdout.strip()
        if out.returncode == 0 and p and os.path.exists(p):
            return p
    except Exception:
        pass
    return "/usr/bin/lldb" if os.path.exists("/usr/bin/lldb") else None


def _find_wechat_main_pid() -> int | None:
    """精确匹配进程名取微信主进程 PID (pgrep -x, 排除 WeChatAppEx 等 helper)。"""
    for name in ("Weixin", "WeChat", "weixin", "wechat"):
        try:
            out = subprocess.run(["pgrep", "-x", name], capture_output=True, text=True, timeout=5)
        except Exception:
            continue
        for line in out.stdout.split():
            if line.strip().isdigit():
                return int(line.strip())
    return None


def extract_db_keys_lldb(
    timeout: int = 40,
    on_log: Callable[[str], None] | None = None,
) -> tuple[str | None, list[str]]:
    """微信 4.1.x: attach **运行中**的微信, lldb 断点 CommonCrypto 抓数据库密钥.

    返回 (passphrase_hex | None, [enc_key_hex, ...]):
      * passphrase: 通用口令 (CCKeyDerivationPBKDF, 仅开新库时出现); 拿到可派生所有库。
      * enc_keys:   运行中读库时从 AES (CCCryptorCreate) 抓到的逐库 enc_key 候选,
                    需上层用每库 salt 的 HMAC 校验认领。
    至少其一非空即算成功; 两者皆空抛 RuntimeError。

    为什么 attach 运行中的微信 (而非 --waitfor 等启动):
      4.1.10 静态分析确认 DB 引擎 roam_server/roam_migration 用 CommonCrypto, 全在主进程;
      --waitfor 早期 attach 对共享缓存符号下断会延迟解析, 错过登录时的开库。attach 已登录
      的微信则符号立即就位, 且库已打开正被读取, 用户滚动聊天即持续触发 AES 命中。

    前置: 微信已「重签名」(get-task-allow) 且**正在运行且已登录**。
    交互: 调用期间用户需在微信里滚动聊天/打开会话以触发数据库读取 (AES 命中收 enc_key)。

    ⚠️ 风险: attach 调试器到微信主进程是风控可检测的高风险行为, 可能延迟触发安全验证/封号
    (见 ylytdeng/wechat-decrypt#126)。强烈建议仅用小号; 本函数一次性 + 命中足够/超时立即 detach。

    参数:
        timeout: lldb 抓取窗口秒数 (attach 持续时长上限, 越短越安全)。
        on_log:  可选日志回调。

    异常:
        RuntimeError: 前缀 NO_WECHAT / RESIGN_NEEDED / NO_LLDB / NO_SYMBOL / CAPTURE_FAILED。
    """
    if not IS_MACOS:
        raise RuntimeError("scanner_mac 仅 macOS 可用")
    log = on_log or (lambda *_a, **_k: None)

    pid = _find_wechat_main_pid()
    if not pid:
        raise RuntimeError("NO_WECHAT: 未找到运行中的微信主进程, 请先打开并登录微信")
    lldb_bin = _find_lldb()
    if not lldb_bin:
        raise RuntimeError("NO_LLDB: 未找到 lldb, 请先安装 Xcode Command Line Tools (xcode-select --install)")
    script = _lldb_capture_path()
    if not script.exists():
        raise RuntimeError(f"CAPTURE_FAILED: lldb 抓取脚本缺失: {script}")

    log(f"lldb={lldb_bin} attach pid={pid} timeout={timeout}s (一次性, 命中足够/超时即 detach)")
    log("流程: 保持微信登录; 点击后请在微信里【滚动聊天列表 / 打开几个会话】约 10-15s 触发数据库读取")
    # lldb -b 结束时即使脚本未显式 detach 也会自动 detach (不杀微信)。
    cmd = [
        lldb_bin, "-b",
        "-o", f"process attach --pid {pid}",
        "-o", f"command script import {script}",
        "-o", f"wx_capture {timeout}",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 30)
    except subprocess.TimeoutExpired:
        raise RuntimeError("CAPTURE_FAILED: lldb 超时未返回; 请重试或改用「手动口令」")

    out = (proc.stdout or "") + "\n" + (proc.stderr or "")
    passphrase: str | None = None
    enckeys: list[str] = []
    for line in out.splitlines():
        s = line.strip()
        if s.startswith("WXLLDB:"):
            log(s)
        elif s.startswith("WECHAT_PASSPHRASE="):
            passphrase = s[len("WECHAT_PASSPHRASE="):].strip() or None
        elif s.startswith("WECHAT_ENCKEY="):
            k = s[len("WECHAT_ENCKEY="):].strip()
            if k:
                enckeys.append(k)
    if passphrase or enckeys:
        return passphrase, enckeys

    low = out.lower()
    if "no_symbol" in low:
        raise RuntimeError(
            "NO_SYMBOL: 未在微信进程定位到 CommonCrypto 符号 (CCCryptorCreate/CCKeyDerivationPBKDF)。"
            "请把日志反馈给开发者。"
        )
    if ("attach failed" in low or "unable to attach" in low or "operation not permitted" in low
            or "not allowed to attach" in low):
        raise RuntimeError("RESIGN_NEEDED: lldb attach 失败。请先「重签名微信」(get-task-allow) 并重启微信后重试。")
    raise RuntimeError(
        "CAPTURE_FAILED: 未抓到密钥。请在点击后到微信【滚动聊天/打开会话】触发数据库读取再重试; 详见日志。"
    )
