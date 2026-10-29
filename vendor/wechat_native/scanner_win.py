"""Windows 微信进程内存扫描.

职责:
  在 Windows 上通过 ctypes 直接调用 Win32 API 读取 Weixin.exe 进程内存,
  提取 SQLCipher 加密密钥 (enc_key). 不依赖任何外部 binary 或第三方注入工具.

平台差异:
  * Windows 使用 ReadProcessMemory, 普通管理员即可访问微信进程, 无需重签名.
  * macOS 对应实现见 scanner_mac.py, 须先用 codesign 追加 get-task-allow.
  * 非 Windows 环境 import 本模块时 ctypes.windll 不存在, 通过 try/except 静默处理,
    保证跨平台 import 不报错, 但调用 extract_keys 时会抛 RuntimeError.

被谁调用:
  * api/wechat.py 的 FastAPI 路由 /api/wechat/extract_keys (Windows 分支)

公开接口:
  extract_keys(db_dir) -> dict[str, str]   # salt_hex -> enc_key_hex
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from typing import Optional

from .key_scan import collect_db_files, scan_buffer, scan_buffer_raw

# 仅 Windows 时 import ctypes (其他平台 import 模块仍要不报错)
try:  # noqa: SIM105 - 避免运行时 raise
    import ctypes
    import ctypes.wintypes as wt
except Exception:  # pragma: no cover - 非 Windows
    ctypes = None  # type: ignore[assignment]
    wt = None  # type: ignore[assignment]


# VirtualQueryEx 返回的 State 字段: 0x1000 表示已提交 (MEM_COMMIT), 仅已提交页才可读写
MEM_COMMIT = 0x1000
# 可读内存保护标志 (PAGE_READONLY / PAGE_READWRITE / PAGE_WRITECOPY 及各 EXECUTE_* 变体)
# 完整列表见 MSDN MEMORY_BASIC_INFORMATION.Protect
READABLE_PROTECTS = {0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80}
# 进程访问权限: PROCESS_VM_READ (0x0010) | PROCESS_QUERY_INFORMATION (0x0400)
# 足够枚举内存区域 + 读取内存, 不需要 PROCESS_ALL_ACCESS
PROC_ACCESS = 0x0010 | 0x0400


def _kernel32():
    """返回 kernel32 DLL 句柄, 非 Windows 时抛 RuntimeError."""
    if ctypes is None:
        raise RuntimeError("scanner_win 仅在 Windows 可用")
    return ctypes.windll.kernel32


class _MBI(ctypes.Structure if ctypes else object):  # type: ignore[misc]
    """MEMORY_BASIC_INFORMATION 结构体 (64 位布局).

    与 Win32 SDK 中的定义对应, 64 位进程下 BaseAddress/AllocationBase/RegionSize
    均为 8 字节, 中间有 DWORD 对齐 padding.
    """
    if ctypes is not None:
        _fields_ = [
            ("BaseAddress", ctypes.c_uint64),
            ("AllocationBase", ctypes.c_uint64),
            ("AllocationProtect", wt.DWORD),
            ("_pad1", wt.DWORD),         # 64 位对齐 padding
            ("RegionSize", ctypes.c_uint64),
            ("State", wt.DWORD),
            ("Protect", wt.DWORD),
            ("Type", wt.DWORD),
            ("_pad2", wt.DWORD),         # 结构体末尾对齐 padding
        ]


def _list_weixin_pids() -> list[tuple[int, int]]:
    """枚举所有 Weixin.exe 进程, 返回按内存占用降序排列的 [(pid, mem_kb), ...].

    使用 tasklist /FO CSV 解析, 内存列格式为 "xxx,xxx K" (含千位分隔符).
    微信通常会启动多个子进程 (渲染进程 / GPU 进程等), 内存最大的通常是主进程,
    主进程堆里最可能持有 enc_key, 因此优先扫描.

    返回:
        [(pid, mem_kb), ...] 按 mem_kb 降序, 若微信未运行则返回空列表.
    """
    out = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq Weixin.exe", "/FO", "CSV", "/NH"],
        capture_output=True, text=True, timeout=15,
    )
    pids: list[tuple[int, int]] = []
    for line in (out.stdout or "").strip().splitlines():
        # CSV 格式: "name","pid","session","session#","mem K"
        cells = line.strip('"').split('","')
        if len(cells) < 5:
            continue
        try:
            pid = int(cells[1])
            mem_kb = int(cells[4].replace(",", "").replace(" K", "").strip() or "0")
        except ValueError:
            continue
        pids.append((pid, mem_kb))
    pids.sort(key=lambda x: x[1], reverse=True)
    return pids


def _read_mem(h, addr: int, size: int) -> Optional[bytes]:
    """从目标进程读取一段内存.

    参数:
        h:    OpenProcess 返回的进程句柄.
        addr: 起始虚拟地址.
        size: 期望读取的字节数.

    返回:
        实际读到的字节串 (长度可能小于 size); 读取失败返回 None
        (例如页面在调用间隙被释放, 属正常情况, 调用方应跳过).
    """
    k = _kernel32()
    buf = ctypes.create_string_buffer(size)  # type: ignore[union-attr]
    n = ctypes.c_size_t(0)  # type: ignore[union-attr]
    ok = k.ReadProcessMemory(h, ctypes.c_uint64(addr), buf, size, ctypes.byref(n))  # type: ignore[union-attr]
    if not ok:
        return None
    return buf.raw[: n.value]


def _enum_regions(h) -> list[tuple[int, int]]:
    """枚举进程中所有可读、已提交、< 500MB 的内存区域.

    通过循环调用 VirtualQueryEx 遍历整个用户态地址空间 (0 ~ 0x7FFF_FFFF_FFFF).
    过滤条件:
      * State == MEM_COMMIT: 已分配并提交到物理/换页文件的页
      * Protect in READABLE_PROTECTS: 页有读权限
      * RegionSize < 500MB: 排除映射的超大文件 (dll / mmap 等)

    参数:
        h: OpenProcess 返回的进程句柄.

    返回:
        [(base_addr, region_size), ...] 列表.
    """
    k = _kernel32()
    regions: list[tuple[int, int]] = []
    addr = 0
    mbi = _MBI()
    while addr < 0x7FFF_FFFF_FFFF:
        # VirtualQueryEx 返回 0 表示查询失败 (通常是超出有效地址范围), 终止循环
        if k.VirtualQueryEx(h, ctypes.c_uint64(addr), ctypes.byref(mbi), ctypes.sizeof(mbi)) == 0:  # type: ignore[union-attr]
            break
        if (
            mbi.State == MEM_COMMIT
            and mbi.Protect in READABLE_PROTECTS
            and 0 < mbi.RegionSize < 500 * 1024 * 1024
        ):
            regions.append((mbi.BaseAddress, mbi.RegionSize))
        nxt = mbi.BaseAddress + mbi.RegionSize
        # 防止地址回绕 (理论上不会, 但作防御)
        if nxt <= addr:
            break
        addr = nxt
    return regions


def _scan_pids_raw(k, pids, db_files, key_map, remaining, on_found, log) -> None:
    """对所有进程重扫内存, 用 raw 32 字节候选认领 enc_key (微信 4.1+)。

    raw key 只在微信**已打开过对应库** (点开聊天/联系人) 后才载入 SQLCipher codec, 故扫前
    最好让用户在微信里点开一个聊天。逐区域扫描, 每 ~64MB 报一次进度。
    """
    for pid, _mem in pids:
        h = k.OpenProcess(PROC_ACCESS, False, pid)
        if not h:
            continue
        try:
            regions = _enum_regions(h)
            log(f"raw 扫描 PID={pid}: {len(regions)} 区域")
            done_mb = 0.0
            next_report = 64.0
            for base, size in regions:
                buf = _read_mem(h, base, size)
                if buf:
                    scan_buffer_raw(buf, db_files, key_map, remaining, on_found=on_found, step=8)
                done_mb += size / (1024 * 1024)
                if done_mb >= next_report:
                    log(f"  raw 扫描进度 PID={pid}: {done_mb:.0f}MB, 已命中 {len(key_map)}")
                    next_report += 64.0
                if not remaining:
                    break
        finally:
            k.CloseHandle(h)
        if not remaining:
            break


def extract_keys(db_dir: str, on_log=None) -> dict[str, str]:
    """扫描所有 Weixin.exe 进程内存, 提取 SQLCipher 加密密钥.

    内部流程:
      1. collect_db_files 枚举 db_dir 下所有 .db 文件并读取 salt (page1 前 16 字节).
      2. _list_weixin_pids 获取所有 Weixin.exe PID, 按内存降序排列 (主进程优先).
      3. 对每个进程: OpenProcess -> 枚举可读内存区域 -> ReadProcessMemory ->
         scan_buffer 在内存块中正则匹配 x'<hex>' 模式 -> HMAC 验证候选密钥.
      4. 所有 salt 均找到 enc_key 时提前退出循环.

    参数:
        db_dir: 微信数据库目录路径.
        on_log: 可选日志回调, 签名为 (msg: str) -> None.

    返回:
        {salt_hex: enc_key_hex} 字典, 每个 enc_key 均已通过 HMAC-SHA512 验证.

    异常:
        RuntimeError: 非 Windows 平台调用 / db_dir 无 .db 文件 / 微信未运行.
                      注意: 若部分进程 OpenProcess 失败但其他进程成功, 不抛异常而是
                      在日志中记录 [WARN] 并继续, 最终可能返回不完整的 key_map.
    """
    if ctypes is None:
        raise RuntimeError("scanner_win.extract_keys 只能在 Windows 上运行")

    log = on_log or (lambda *_args, **_kw: None)
    db_files, salt_map = collect_db_files(db_dir)
    if not salt_map:
        raise RuntimeError(f"未在 {db_dir} 找到任何 .db 文件")

    pids = _list_weixin_pids()
    if not pids:
        raise RuntimeError("Weixin.exe 未运行, 请先打开并登录微信")

    log(f"找到 {len(db_files)} 个数据库, {len(salt_map)} 个 salt; {len(pids)} 个 Weixin.exe 进程")

    key_map: dict[str, str] = {}
    # remaining 跟踪尚未找到 enc_key 的 salt, 全部命中时提前退出
    remaining = set(salt_map.keys())
    started = time.time()

    def on_found(salt: str, key: str, dbs: list[str]) -> None:
        """处理 on found 业务逻辑，保持输入、输出与异常语义清晰。"""
        log(f"[FOUND] salt={salt} key={key} dbs={','.join(dbs)}")

    k = _kernel32()
    # 阶段 1: x'<hex>' 文本模式扫描 (微信 4.0/3.x, 快)
    for pid, _mem in pids:
        # PROC_ACCESS = PROCESS_VM_READ | PROCESS_QUERY_INFORMATION
        h = k.OpenProcess(PROC_ACCESS, False, pid)
        if not h:
            # 普通管理员通常能打开微信进程; 失败多见于保护模式子进程, 继续尝试下一个
            log(f"[WARN] OpenProcess({pid}) 失败 (errno={ctypes.get_last_error()})")  # type: ignore[union-attr]
            continue
        try:
            regions = _enum_regions(h)
            log(f"扫描 PID={pid}: {len(regions)} 区域 (文本模式)")
            for base, size in regions:
                buf = _read_mem(h, base, size)
                if not buf:
                    continue
                scan_buffer(buf, db_files, salt_map, key_map, remaining, on_found=on_found)
                # 所有 salt 均已找到 enc_key, 提前退出内层循环
                if not remaining:
                    break
        finally:
            k.CloseHandle(h)
        # 所有 salt 均已找到, 无需扫描其他进程
        if not remaining:
            break

    # 阶段 2: raw 字节扫描 (微信 4.1+: codec 里的 raw key 不以 x'<hex>' 文本出现,
    # 文本模式全部 0 命中后才扫, 较慢; 依赖微信已打开过对应库把 key 载入 codec)
    if remaining:
        log(f"文本模式命中 {len(key_map)}/{len(salt_map)}; 剩 {len(remaining)} 个 salt 未命中 —— "
            f"微信很可能是 4.1+, 改扫 raw 字节 (SQLCipher codec key, 较慢)")
        _scan_pids_raw(k, pids, db_files, key_map, remaining, on_found, log)

    elapsed = time.time() - started
    log(f"扫描结束: {len(key_map)}/{len(salt_map)} 命中, 耗时 {elapsed:.1f}s")
    return key_map


# ─── 主动 (调试器) 抓 key ─────────────────────────────────────────
# 微信 4.1.10.31+ 起, raw key 不再在内存里缓存, 上面 raw 扫描必然 0 命中.
# 用本地 vendored 的 active_key 调试器方法抓 master key:
#   1. 静态在 Weixin.dll .text 段搜字节签名 24 50 48 C7 45 ?? FE FF FF FF 44 89 CF
#      44 89 C3 49 89 D6 找 WCDB cipher-config 函数 (>=1 候选)
#   2. CreateProcessW(DEBUG_PROCESS) 启动新微信实例, 设 INT3 断点
#   3. 微信登录打开 db 时断点命中, 从 [rdx+0x08] 读 32 字节 master key
#   4. HMAC oracle 验证: 试 raw-key + PBKDF2(master, salt, 256000, 32) 两种 mode
# 抓到的 32 字节是 master key (per-install stable), 解每个 db 时再 PBKDF2 派生.
#
# ⚠ 账号封禁风险: 调试器方法需要关闭现有微信 → 重启一个 DEBUG 实例 →
# 用户在新窗口重新登录 (扫码/密码). 微信风控会记录"非用户主动行为"的
# 进程启动 + 调试器附加, 极少数账号可能被临时风控 (限制登录/要求好友
# 辅助验证). 强烈建议用小号 (wxid 开头的) 验证, **不要用主号/常用工作号**.


def _derive_page_key(master_key: bytes, salt: bytes) -> bytes:
    """微信 4.1.10.31+ password mode: PBKDF2-HMAC-SHA512(master, salt, 256000, 32).
    跟 wechat_native/crypto.derive_key 同源.
    """
    import hashlib
    return hashlib.pbkdf2_hmac("sha512", master_key, salt, 256000, dklen=32)


def _hmac_check_pagekey(page_key: bytes, db_page1: bytes) -> bool:
    """SQLCipher 4 page-1 HMAC 验证. 跟 wechat_native.key_scan.verify_enc_key 同源."""
    from .crypto import PAGE_SZ, SALT_SZ
    import hashlib
    import hmac
    import struct
    if len(page_key) != 32 or len(db_page1) < PAGE_SZ:
        return False
    salt = db_page1[:SALT_SZ]
    mac_salt = bytes(b ^ 0x3A for b in salt)
    mac_key = hashlib.pbkdf2_hmac("sha512", page_key, mac_salt, 2, dklen=32)
    h = hmac.new(mac_key, db_page1[SALT_SZ: PAGE_SZ - 64], hashlib.sha512)
    h.update(struct.pack("<I", 1))
    return h.digest() == db_page1[PAGE_SZ - 64: PAGE_SZ]


def _weixin_install_dir_from_registry() -> Optional[str]:
    """注册表读微信 4.x 安装目录 (与 scripts/windows_wechat_get_key.ps1 同一来源).

    客户安装微信时改过盘符/目录 (没装 C 盘默认位置, 2026-10 客户实案) 时,
    硬编码的 C:\\Program Files 候选全落空, 必须先查注册表。
    """
    try:
        import winreg
    except ImportError:
        return None
    for hive, subkey in (
        (winreg.HKEY_CURRENT_USER, r"Software\Tencent\Weixin"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Tencent\Weixin"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Tencent\Weixin"),
    ):
        try:
            with winreg.OpenKey(hive, subkey) as k:
                for value_name in ("InstallPath", "InstallDir", "Install_Dir"):
                    try:
                        val, _ = winreg.QueryValueEx(k, value_name)
                    except OSError:
                        continue
                    if val and os.path.isdir(str(val)):
                        return str(val)
        except OSError:
            continue
    return None


def _find_wechat_exe_and_dll() -> tuple[str, str] | None:
    """找 Weixin.exe + Weixin.dll 路径 (注册表优先, C 盘默认目录兜底, 兼容版本子目录)."""
    import os
    candidates: list[str] = []
    reg_dir = _weixin_install_dir_from_registry()
    if reg_dir:
        candidates.append(reg_dir)
    candidates += [
        r"C:\Program Files (x86)\Tencent\Weixin",
        r"C:\Program Files\Tencent\Weixin",
    ]
    for root in candidates:
        if not os.path.isdir(root):
            continue
        exe = os.path.join(root, "Weixin.exe")
        if not os.path.isfile(exe):
            continue
        # 版本子目录 (4.1.10.53 这种)
        for entry in sorted(os.listdir(root), reverse=True):
            sub = os.path.join(root, entry, "Weixin.dll")
            if os.path.isfile(sub):
                return exe, sub
        cand = os.path.join(root, "Weixin.dll")
        if os.path.isfile(cand):
            return exe, cand
    return None


def _list_running_weixin_pids() -> list[int]:
    """列出当前在跑的 Weixin.exe 进程 PID (无副作用, 不杀任何进程)."""
    import subprocess
    out = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq Weixin.exe", "/FO", "CSV", "/NH"],
        capture_output=True, text=True, timeout=15,
    )
    pids: list[int] = []
    for line in (out.stdout or "").strip().splitlines():
        cells = line.strip('"').split('","')
        if len(cells) < 2:
            continue
        try:
            pids.append(int(cells[1]))
        except ValueError:
            continue
    return pids


WEIXIN_RUNNING_HINT = (
    "检测到微信正在运行 (PID: {pids}).\n"
    "调试器方法需要独占 Weixin.exe, 但**我们不会自动退出微信** (避免丢失未保存的"
    "草稿/聊天记录). 请你先:\n"
    "  1. 打开微信 → 等聊天记录同步完成 (左下角同步图标消失)\n"
    "  2. 微信右上角 X 关闭 (主窗口关闭后, 微信常驻进程仍在)\n"
    "  3. 右键任务栏微信图标 → 退出 (确保 5 个子进程全清)\n"
    "  4. 再点「自动获取密码」\n"
    "确认无 Weixin.exe 后重试."
)


def extract_keys_active(db_dir: str, on_log=None) -> dict[str, str]:
    """处理 extract keys active 业务逻辑，保持输入、输出与异常语义清晰。"""
    import os
    """微信 4.1.10.31+ 主动 (调试器) 抓 master key + PBKDF2 派生每个 db 的 page key.

    流程:
      1. 找 Weixin.exe / Weixin.dll 路径
      2. 找 db_dir/message/message_0.db (HMAC oracle)
      3. 调本地 active_key.extract_wechat_key_active 抓 master key
         (启动 DEBUG 实例, 等待用户在新窗口登录, 断点命中)
      4. 用 master key + PBKDF2-HMAC-SHA512 派生每个 db 的 page key
      5. HMAC oracle 验证每个 page key
      6. 返回 {salt_hex: page_key_hex}

    ⚠ 账号封禁风险: 见本模块顶部注释. 强烈建议用小号.

    参数:
        db_dir: 微信数据库目录路径 (含 message/message_0.db).
        on_log: 可选日志回调, 签名为 (msg: str) -> None.

    返回:
        {salt_hex: page_key_hex} 字典, 每个 page_key 均已通过 HMAC 验证.

    异常:
        RuntimeError: 非 Windows / 找不到 Weixin.exe 或 message_0.db / 抓 key 失败.
    """
    if ctypes is None:
        raise RuntimeError("scanner_win.extract_keys_active 只能在 Windows 上运行")

    log = on_log or (lambda *_args, **_kw: None)

    # 0. 安全检查: 检测微信是否在跑, 在跑就拒绝 (避免脚本自动杀进程导致数据丢失)
    running = _list_running_weixin_pids()
    if running:
        raise RuntimeError(WEIXIN_RUNNING_HINT.format(pids=", ".join(str(p) for p in running)))

    # 1. 找 weixin 路径
    info = _find_wechat_exe_and_dll()
    if not info:
        raise RuntimeError("未找到 Weixin.exe (注册表 Software\\Tencent\\Weixin 与 "
                           "C:\\Program Files* 默认目录均无; 微信装在非默认位置时,"
                           "请确认安装器写入了注册表 InstallPath)")
    weixin_exe, weixin_dll = info
    log(f"找到 Weixin.exe: {weixin_exe}")
    log(f"找到 Weixin.dll: {weixin_dll}")

    # 2. 找 HMAC oracle 库
    db_oracle = os.path.join(db_dir, "message", "message_0.db")  # type: ignore[name-defined]
    if not os.path.isfile(db_oracle):
        raise RuntimeError(f"未找到 HMAC 校验库: {db_oracle}")
    log(f"HMAC oracle 库: {db_oracle}")

    # 3. 调本地 vendored 的 active_key 抓 master key
    from .active_key import extract_wechat_key_active

    log("✅ 微信已确认关闭. 接下来调试器会:")
    log("   1. 启动新的 DEBUG 实例 (新窗口弹出)")
    log("   2. 等待你在新窗口里扫码/密码登录")
    log("   3. 微信打开 db 时断点命中 → 抓 master key → 退出（抓完密钥会自动退出，后续使用重新登一下即可）")
    log("⚠ 这一步**需要管理员权限** (会自动 UAC 提权)")
    log("⚠ 强烈建议用**小号** (wxid_*), 主号/常用工作号有封禁风险")

    master_key = extract_wechat_key_active(
        weixin_dll=weixin_dll,
        db_path=db_oracle,
        analyze_only=False,
        timeout=600,
    )
    if not master_key or len(master_key) != 32:
        raise RuntimeError(
            "extract_wechat_key_active 未抓到 master key (未在弹出的微信里登录 / "
            "UAC 拒绝 / 微信版本不兼容)"
        )
    log(f"✅ 抓到 master key: {master_key.hex()}")

    # 4. 派生每个 db 的 page key + HMAC 验证
    db_files, _salt_map = collect_db_files(db_dir)
    if not db_files:
        raise RuntimeError(f"未在 {db_dir} 找到任何 .db 文件")

    key_map: dict[str, str] = {}
    n_ok = 0
    n_fail = 0
    for db in db_files:
        try:
            page_key = _derive_page_key(master_key, db.page1[:16])
        except Exception as e:
            log(f"  ⚠ {db.rel}: PBKDF2 派生失败: {e}")
            n_fail += 1
            continue
        if not _hmac_check_pagekey(page_key, db.page1):
            log(f"  ⚠ {db.rel}: HMAC 验证失败 (salt={db.salt_hex[:16]}…)")
            n_fail += 1
            continue
        key_map[db.salt_hex] = page_key.hex()
        n_ok += 1
    log(f"派生完成: {n_ok}/{len(db_files)} 命中, 失败 {n_fail}")

    if n_ok == 0:
        raise RuntimeError("没有任何 db 派生成功, master key 可能不对")

    return key_map
