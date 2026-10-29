"""macOS 内存扫描 helper -- 独立脚本, 只依赖 Python stdlib.

职责:
  以 root 权限运行, 通过 macOS Mach API (task_for_pid / mach_vm_read) 扫描
  微信进程的虚拟内存, 从中提取 SQLCipher 数据库密钥 (db-key 模式) 或
  图片 AES 密钥 (image-key 模式).

平台限制:
  * 仅限 macOS (使用 libSystem 的 Mach/CommonCrypto API).
  * 必须由 root 启动才能对其他进程调用 task_for_pid.
  * 典型调用方: wechat_native/wechat.py 通过 osascript "do shell script
    ... with administrator privileges" 调起本脚本.

解释器限制:
  * 必须用 /usr/bin/python3 (macOS Command Line Tools 自带的系统解释器),
    不能是 venv / PyInstaller bundle.
  * 因此本脚本不能 import 项目其他模块, 也不能用 pycryptodome / fastapi.
  * HMAC 验证只用 hashlib (stdlib 自带 PBKDF2-SHA512).
  * AES 通过 ctypes 调系统 libSystem 里的 CCCrypt (CommonCrypto).

运行模式:
  --mode db-key (默认): 扫 SQLCipher 数据库密钥. 输入 --db-dir.
  --mode image-key:     扫图片 AES 密钥. 输入 --dat-dir.
                        参考 ylytdeng/wechat-decrypt 的 pattern-based 批量验证思路.

输入:
  argv: --mode <db-key|image-key> --db-dir/--dat-dir <path> [--limit-mb 500]
输出:
  stdout: JSON {"keys": {pattern_hex: key_hex}, "log": [...], "error": null|"..."}
退出码:
  0 = 成功 (即使一个 key 都没扫到也是 0)
  1 = 致命错误
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import hashlib
import hmac
import json
import os
import re
import struct
import subprocess
import sys
import time

# SQLCipher 第一页大小 (固定 4096 字节)
PAGE_SZ = 4096
# SQLCipher salt 长度: 位于第一页开头 16 字节
SALT_SZ = 16
# AES-256 密钥长度 (字节)
KEY_SZ = 32
# HMAC-SHA512 摘要长度 (字节); SQLCipher 尾部 reserve = IV(16) + HMAC(64)
HMAC_LEN = 64
# 正则: 匹配内存中 x'...hex...' 形式的密钥字面量 (64~192 个十六进制字符)
HEX_RE = re.compile(rb"x'([0-9a-fA-F]{64,192})'")

# ─── mach API ─────────────────────────────────────
# macOS libc (libSystem) 通过 ctypes 加载; Mach / CommonCrypto 符号均在其中
libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)

# mach_task_self() 返回当前进程的 mach port (用作 task_for_pid 的 target_tport)
mach_task_self = libc.mach_task_self
mach_task_self.restype = ctypes.c_uint32

# kern_return_t task_for_pid(mach_port_name_t target_tport, int pid, mach_port_name_t *task)
# 获取目标进程的 task port, 需要 root 权限或 com.apple.security.cs.allow-jit entitlement
libc.task_for_pid.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.POINTER(ctypes.c_uint32)]
libc.task_for_pid.restype = ctypes.c_int

# struct vm_region_basic_info_data_64_t (9 int32 words = 36 bytes)
# flavor 值和 count 值均为 9; info[0] 是 vm_prot_t protection 字段
VM_REGION_BASIC_INFO_64 = 9
VM_REGION_BASIC_INFO_COUNT_64 = 9
# 内存区域保护标志: bit 0 = 可读
VM_PROT_READ = 0x1

# mach_vm_region: 枚举目标 task 的虚拟内存区域, 返回区域起始地址/大小/属性
libc.mach_vm_region.argtypes = [
    ctypes.c_uint32,                         # task
    ctypes.POINTER(ctypes.c_uint64),         # &addr
    ctypes.POINTER(ctypes.c_uint64),         # &size
    ctypes.c_int,                            # flavor
    ctypes.POINTER(ctypes.c_int * VM_REGION_BASIC_INFO_COUNT_64),
    ctypes.POINTER(ctypes.c_uint32),         # &count
    ctypes.POINTER(ctypes.c_uint32),         # &object_name
]
libc.mach_vm_region.restype = ctypes.c_int

# mach_vm_read: 从目标 task 虚拟地址空间读取 size 字节到本进程
# 返回的 data 是 vm_offset_t (内核映射的本进程地址), 用完后必须 mach_vm_deallocate 释放
libc.mach_vm_read.argtypes = [
    ctypes.c_uint32, ctypes.c_uint64, ctypes.c_uint64,
    ctypes.POINTER(ctypes.c_uint64),         # &data (vm_offset_t)
    ctypes.POINTER(ctypes.c_uint32),         # &dataCnt
]
libc.mach_vm_read.restype = ctypes.c_int

# mach_vm_deallocate: 释放 mach_vm_read 在本进程映射的内存
libc.mach_vm_deallocate.argtypes = [ctypes.c_uint32, ctypes.c_uint64, ctypes.c_uint64]
libc.mach_vm_deallocate.restype = ctypes.c_int

# Mach API 调用成功的返回码
KERN_SUCCESS = 0


# ─── CommonCrypto (用于图片密钥扫描时的 AES 验证) ───
# CCCrypt 在 libSystem 里, libc 已经能解析符号
try:
    # CCCrypt 是 macOS CommonCrypto 框架的一站式加解密接口
    libc.CCCrypt.argtypes = [
        ctypes.c_uint32,   # CCOperation (kCCDecrypt = 1)
        ctypes.c_uint32,   # CCAlgorithm (kCCAlgorithmAES128 = 0)
        ctypes.c_uint32,   # CCOptions (kCCOptionPKCS7Padding=1, kCCOptionECBMode=2)
        ctypes.c_void_p,   # key
        ctypes.c_size_t,   # keyLength
        ctypes.c_void_p,   # iv (NULL for ECB)
        ctypes.c_void_p,   # dataIn
        ctypes.c_size_t,   # dataInLength
        ctypes.c_void_p,   # dataOut
        ctypes.c_size_t,   # dataOutAvailable
        ctypes.POINTER(ctypes.c_size_t),  # dataOutMoved
    ]
    libc.CCCrypt.restype = ctypes.c_int
    HAS_CCCRYPT = True
except Exception:
    # 部分受限环境下 CCCrypt 符号不可见, 退化为不可用
    HAS_CCCRYPT = False

# CCOperation: kCCDecrypt = 1
CC_DECRYPT = 1
# CCAlgorithm: kCCAlgorithmAES128 = 0 (即 AES, 密钥长度决定是 128/192/256)
CC_AES = 0
# CCOptions: kCCOptionECBMode = 2, 不设 PKCS7Padding (由调用方保证输入对齐)
CC_ECB = 2  # kCCOptionECBMode (no padding bit set: caller-managed)


# ─── 进程定位 ─────────────────────────────────────
def find_wechat_pids() -> list[int]:
    """查找正在运行的微信进程 PID 列表.

    同时兼容 WeChat (3.x 旧版) 和 Weixin (4.x 新版) 的进程名称,
    以及大小写变体 (pgrep -x 精确匹配进程名).

    返回:
        已去重并升序排列的 PID 列表; 若微信未运行则返回空列表.
    """
    pids: list[int] = []
    for name in ("WeChat", "Weixin", "wechat", "weixin"):
        try:
            out = subprocess.run(["pgrep", "-x", name], capture_output=True, text=True, timeout=5)
        except Exception:
            continue
        for line in out.stdout.split():
            line = line.strip()
            if line.isdigit():
                pids.append(int(line))
    return sorted(set(pids))


# ─── HMAC 验证 ────────────────────────────────────
def verify_enc_key(enc_key: bytes, db_page1: bytes) -> bool:
    """验证候选密钥是否与数据库第一页 HMAC 匹配.

    SQLCipher 4 的 HMAC 验证流程:
      1. mac_salt = salt ^ 0x3A  (逐字节异或, 与 cipher_salt 解耦)
      2. mac_key  = PBKDF2-SHA512(enc_key, mac_salt, iter=2, dklen=32)
      3. hmac_data = page1[SALT_SZ : PAGE_SZ - HMAC_LEN]
      4. computed  = HMAC-SHA512(mac_key, hmac_data || pack('<I', 1))
      5. stored    = page1[PAGE_SZ - HMAC_LEN : PAGE_SZ]
      6. 返回 computed == stored

    参数:
        enc_key: 候选 AES-256 密钥, 32 字节.
        db_page1: 数据库第一页原始字节, 至少 PAGE_SZ(4096) 字节.

    返回:
        True 表示密钥正确; False 表示不匹配或数据不足.
    """
    if len(db_page1) < PAGE_SZ:
        return False
    salt = db_page1[:SALT_SZ]
    # mac_salt 由 cipher_salt 每字节异或 0x3A 生成, 避免 mac_key 与 enc_key 复用相同盐
    mac_salt = bytes(b ^ 0x3A for b in salt)
    # 仅需 2 次迭代的 PBKDF2 (SQLCipher mac_key 规范), 与 cipher_key 的 256000 次不同
    mac_key = hashlib.pbkdf2_hmac("sha512", enc_key, mac_salt, 2, dklen=KEY_SZ)
    # HMAC 覆盖范围: salt 之后到 HMAC 存储位置之前的全部内容
    hmac_data = db_page1[SALT_SZ: PAGE_SZ - HMAC_LEN]
    stored = db_page1[PAGE_SZ - HMAC_LEN: PAGE_SZ]
    h = hmac.new(mac_key, hmac_data, hashlib.sha512)
    # 附加小端序页号 1 (SQLCipher 规范: HMAC 输入末尾追加 4 字节页号)
    h.update(struct.pack("<I", 1))
    return h.digest() == stored


# ─── DB 收集 ──────────────────────────────────────
def collect_dbs(db_dir: str):
    """递归扫描目录, 收集有效的 SQLCipher 加密数据库文件.

    过滤条件:
      * 文件名以 .db 结尾, 排除 WAL(-wal) 和 SHM(-shm) 辅助文件.
      * 文件大小 ≥ PAGE_SZ (4096 字节), 否则没有完整第一页无法验证.

    参数:
        db_dir: 微信数据库目录路径 (通常为 ~/Library/Containers/com.tencent.xinWeChat/...).

    返回:
        files: list[dict], 每项包含 path/salt/page1/rel 字段.
        salts: dict[salt_hex -> list[rel_path]], 按 salt 分组便于密钥匹配.

    副作用: 无 (只读文件).
    """
    files = []
    salts: dict[str, list[str]] = {}
    for root, _dirs, names in os.walk(db_dir):
        for name in names:
            if not name.endswith(".db") or name.endswith(("-wal", "-shm")):
                continue
            full = os.path.join(root, name)
            try:
                size = os.path.getsize(full)
            except OSError:
                continue
            if size < PAGE_SZ:
                continue
            try:
                with open(full, "rb") as f:
                    page1 = f.read(PAGE_SZ)
            except OSError:
                continue
            # salt 是第一页最开头的 16 字节, 以十六进制字符串作为唯一标识
            salt = page1[:SALT_SZ].hex()
            rel = os.path.relpath(full, db_dir)
            files.append({"path": full, "salt": salt, "page1": page1, "rel": rel})
            salts.setdefault(salt, []).append(rel)
    return files, salts


# ─── 内存扫描 ─────────────────────────────────────
def task_for_pid(pid: int) -> int:
    """通过 Mach API 获取目标进程的 task port.

    task port 是后续 mach_vm_region / mach_vm_read 的必要句柄.
    需要 root 权限; 普通用户调用会返回 KERN_FAILURE (5).

    参数:
        pid: 目标进程 PID (微信进程).

    返回:
        task port 值 (uint32).

    异常:
        RuntimeError: 内核调用失败, 附带 kr 错误码和 errno.
    """
    task = ctypes.c_uint32(0)
    rc = libc.task_for_pid(mach_task_self(), pid, ctypes.byref(task))
    if rc != KERN_SUCCESS:
        raise RuntimeError(f"task_for_pid({pid}) kr={rc} errno={ctypes.get_errno()}")
    return task.value


def enum_readable_regions(task: int, max_size: int = 500 * 1024 * 1024):
    """枚举目标 task 中所有可读的虚拟内存区域.

    使用 mach_vm_region 从地址 0 开始逐区域遍历, 只 yield 满足:
      * protection 包含 VM_PROT_READ (bit 0)
      * 区域大小在 (0, max_size) 范围内 (跳过超大匿名映射, 防止超时)

    参数:
        task: 由 task_for_pid 获取的 task port.
        max_size: 单个区域的大小上限 (字节), 默认 500 MB; 防止扫描 GPU/VRAM 映射挂起.

    生成:
        (addr: int, size: int) 元组, 表示可读区域的起始地址和大小.
    """
    addr = ctypes.c_uint64(0)
    while True:
        size = ctypes.c_uint64(0)
        info = (ctypes.c_int * VM_REGION_BASIC_INFO_COUNT_64)()
        cnt = ctypes.c_uint32(VM_REGION_BASIC_INFO_COUNT_64)
        obj = ctypes.c_uint32(0)
        rc = libc.mach_vm_region(
            task, ctypes.byref(addr), ctypes.byref(size),
            VM_REGION_BASIC_INFO_64, info, ctypes.byref(cnt), ctypes.byref(obj),
        )
        if rc != KERN_SUCCESS:
            # KERN_INVALID_ADDRESS 表示已到虚拟地址空间末尾, 正常终止
            break
        prot = info[0]   # vm_prot_t protection
        if (prot & VM_PROT_READ) and 0 < size.value < max_size:
            yield (addr.value, size.value)
        # 移动到下一个区域的起始地址
        new_addr = addr.value + size.value
        if new_addr <= addr.value:
            # 防止地址回绕 (理论上不应发生, 但防御性检查)
            break
        addr.value = new_addr


def read_region(task: int, addr: int, size: int) -> bytes | None:
    """从目标 task 的指定虚拟地址读取 size 字节.

    mach_vm_read 将目标进程内存映射到本进程, 读完后必须调用
    mach_vm_deallocate 释放, 否则本进程地址空间会泄漏.

    参数:
        task: task port.
        addr: 目标进程虚拟地址.
        size: 读取字节数.

    返回:
        成功返回 bytes 拷贝; 失败 (区域不可访问等) 返回 None.

    副作用: 通过 mach_vm_deallocate 释放内核映射的临时内存.
    """
    data = ctypes.c_uint64(0)
    dcnt = ctypes.c_uint32(0)
    rc = libc.mach_vm_read(task, addr, size, ctypes.byref(data), ctypes.byref(dcnt))
    if rc != KERN_SUCCESS:
        return None
    try:
        # data.value 是内核在本进程地址空间映射的临时指针, 直接转 bytes 拷贝
        buf = ctypes.string_at(data.value, dcnt.value)
    finally:
        # 无论成败都必须释放映射, 避免本进程虚拟地址空间耗尽
        libc.mach_vm_deallocate(mach_task_self(), data.value, dcnt.value)
    return buf


def scan_buffer(buf, files, salts, key_map, remaining, diag):
    """在内存 buffer 中扫描 SQLCipher 数据库密钥候选, 并写入 key_map.

    diag 字段:
        by_len: dict[hex_len -> count], 统计各长度 hex 字面量出现次数.
        salt_samples: 前 8 个 96-hex 后 32 位 (用于对比已知 salt 做调试).
        key_samples: 前 8 个 64-hex 候选 (用于调试).
        match_no_hmac: 96-hex 模式命中但 HMAC 验证失败的计数 (旁路兜底次数).

    匹配策略 (与 wechat-cli C 实现对齐):
      * 96 hex: 布局为 enc_key(64) + salt(32). 后 32 hex 与已知 salt 配对,
        配对成功即视为命中 (不等 HMAC); HMAC 单独验证用于标记可信度.
      * >96 hex (偶数长度): 同 96 hex, 取前 64 为 enc_key, 后 32 为 salt.
      * 64 hex: 整体视为 enc_key, 遍历所有已知 salt 做 HMAC 验证, 通过才命中.

    参数:
        buf: 内存区域字节串.
        files: collect_dbs 返回的文件列表.
        salts: salt_hex -> [rel_path] 映射.
        key_map: salt_hex -> key_hex 结果字典 (原地写入).
        remaining: 尚未找到密钥的 salt_hex 集合 (原地删除).
        diag: 诊断信息字典 (原地更新).

    返回:
        本次在 buf 中命中的 hex 字面量总数 (含重复).
    """
    matches = 0
    for m in HEX_RE.finditer(buf):
        hex_str = m.group(1).decode().lower()
        matches += 1
        n = len(hex_str)
        diag["by_len"][n] = diag["by_len"].get(n, 0) + 1

        if n == 96 or (n > 96 and n % 2 == 0):
            # 96-hex 布局: [enc_key 64 hex][salt 32 hex]
            enc_hex = hex_str[:64]
            salt_hex = hex_str[64:96] if n == 96 else hex_str[-32:]
            if len(diag["salt_samples"]) < 8:
                diag["salt_samples"].append(salt_hex)
            if salt_hex in remaining:
                try:
                    enc_key = bytes.fromhex(enc_hex)
                except ValueError:
                    continue
                for f in files:
                    if f["salt"] == salt_hex:
                        # 先 HMAC, 失败也按 C 实现兜底直接入 (旁路标记)
                        ok = verify_enc_key(enc_key, f["page1"])
                        if not ok:
                            diag["match_no_hmac"] += 1
                        key_map[salt_hex] = enc_hex
                        remaining.discard(salt_hex)
                        break
        elif n == 64:
            # 64-hex: 纯密钥字面量, 需要 HMAC 验证才能确认匹配哪个数据库
            if len(diag["key_samples"]) < 8:
                diag["key_samples"].append(hex_str)
            if not remaining:
                continue
            try:
                enc_key = bytes.fromhex(hex_str)
            except ValueError:
                continue
            for f in files:
                if f["salt"] in remaining and verify_enc_key(enc_key, f["page1"]):
                    key_map[f["salt"]] = hex_str
                    remaining.discard(f["salt"])
                    break
    return matches


# ─── 图片密钥扫描 (image-key mode) ───────────────
# 微信 4.x V4 格式 .dat 文件的魔数 (前 6 字节)
V4_SIG = b"\x07\x08V2\x08\x07"


def aes_ecb_decrypt(key: bytes, ct: bytes) -> bytes | None:
    """用 CommonCrypto 做 AES-128-ECB 单/多块解密 (无 padding).

    ct 长度必须是 16 的倍数. 失败返回 None.

    参数:
        key: 16 字节 AES-128 密钥.
        ct: 密文, 长度必须是 16 的整数倍.

    返回:
        解密成功返回与 ct 等长的明文字节; 参数非法或 CCCrypt 失败返回 None.
    """
    if not HAS_CCCRYPT or len(key) != 16 or len(ct) == 0 or len(ct) % 16 != 0:
        return None
    out = (ctypes.c_ubyte * len(ct))()
    moved = ctypes.c_size_t(0)
    rc = libc.CCCrypt(
        CC_DECRYPT, CC_AES, CC_ECB,
        key, len(key),
        None,          # ECB 模式不使用 IV
        ct, len(ct),
        out, len(ct),
        ctypes.byref(moved),
    )
    if rc != 0 or moved.value == 0:
        return None
    return bytes(out[: moved.value])


def is_image_magic(data: bytes) -> bool:
    """检查字节序列是否以已知图像格式的合法文件头开始.

    用于验证 AES 解密结果: 若候选密钥正确, 解密出的第一个 16 字节块应包含
    合法的图像魔数. 比简单的 3 字节检查更严格, 以降低假阳性概率.

    支持格式: JPEG, PNG, BMP, WebP, GIF.

    随机 16 字节 AES-ECB 解出 JPEG SOI (FF D8 FF) 的概率约 1/2^24,
    600MB 内存约 3700 万个对齐块, 不加后续字节校验会产生大量假阳性.

    参数:
        data: 待检查字节序列, 建议至少 8 字节.

    返回:
        True 表示匹配已知图像格式头; False 表示不匹配或数据过短.
    """
    if len(data) < 8:
        return False
    # JPEG: FF D8 FF + 第 4 字节是合法 marker (E0=JFIF, E1=EXIF, E2=ICC, DB=DQT, C0=SOF0, C4=DHT 等)
    if data[:3] == b"\xFF\xD8\xFF":
        return data[3] in (0xE0, 0xE1, 0xE2, 0xE3, 0xE8, 0xEE, 0xDB, 0xC0, 0xC4, 0xFE)
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return True
    if data[:2] == b"BM" and len(data) >= 14:
        # BMP: 11-14 字节是 pixel data offset, 合理范围 [14, 0x1000]
        offset = int.from_bytes(data[10:14], "little")
        return 14 <= offset <= 0x1000
    if data[:4] == b"RIFF" and len(data) >= 12 and data[8:12] == b"WEBP":
        return True
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return True
    return False


def collect_dat_patterns(dat_dir: str, max_files: int = 200) -> list[dict]:
    """收集 V4 格式 .dat 文件的前 16 字节 AES 密文作为验证 pattern.

    V4 .dat 文件布局:
      [0:6]   = V4_SIG (魔数)
      [6:15]  = 其他头部字段 (跳过)
      [15:31] = 第一个 AES-ECB 加密块 (16 字节密文, 对应图像文件头)

    同一会话内的多个 .dat 文件使用相同密钥, 因此按密文去重:
    不同 pattern 可独立验证候选密钥, 命中越多越可信.

    参数:
        dat_dir: 微信图片缓存目录.
        max_files: 最多采集的 pattern 数, 默认 200 (平衡覆盖率与扫描耗时).

    返回:
        list[dict], 每项包含: ct(bytes), ct_hex(str), rel(str).
        按密文去重, 同一密文只保留第一个文件.
    """
    seen: dict[str, dict] = {}
    for root, _dirs, names in os.walk(dat_dir):
        for name in names:
            if not name.lower().endswith(".dat"):
                continue
            full = os.path.join(root, name)
            try:
                with open(full, "rb") as f:
                    # 读取魔数(6) + 头部(9) + 第一个加密块(16) = 31 字节
                    head = f.read(15 + 16)
            except OSError:
                continue
            if len(head) < 31 or head[:6] != V4_SIG:
                continue
            # 取偏移 15 开始的 16 字节密文作为 pattern
            ct = head[15:31]
            ct_hex = ct.hex()
            if ct_hex in seen:
                continue
            seen[ct_hex] = {"ct": ct, "ct_hex": ct_hex, "rel": os.path.relpath(full, dat_dir)}
            if len(seen) >= max_files:
                break
        if len(seen) >= max_files:
            break
    return list(seen.values())


# 微信 4.0 把图片 AES 密钥以 32 位 hex ASCII 字符串存在堆里 (与 Windows 端
# wechat_key_extractor 的实现一致), 实际 AES key 取该字符串的前 16 个 ASCII 字节
# (见 dat_to_image.decrypt_v4 的 str(aes_key).encode()[:16]).
# 正则: 匹配内存中连续 32 个十六进制 ASCII 字符 (即候选 AES 密钥的 hex 形式)
HEX32_IMG_RE = re.compile(rb"[0-9a-fA-F]{32}")


def scan_buffer_image(buf: bytes, patterns: list[dict], key_hits: dict[str, int], diag: dict) -> int:
    """在内存 buffer 中扫描图片 AES 密钥候选, 累积每个候选 key 的 pattern 命中数.

    算法思路:
      旧版 16 字节步长滑窗需对 600MB 内存做约 3700 万次 AES, 在 osascript
      600s 时限内必然超时. 改用正则预筛 (典型 buffer 仅数千个 32-hex 候选),
      大幅减少 AES 调用次数.

    命中计数而非提前退出:
      真实密钥可解该会话所有图片 (count 高); 假阳性只能随机解出 1 个
      (count = 1). 上层按 key_hits 排序取高命中者作为权威密钥.

    key 格式说明:
      key_hits 的键是 32-hex ASCII 原文字符串 (保留大小写); 下游
      decrypt_v4 使用 str(aes_key).encode()[:16] 还原真实 key,
      lower()/再编码均会破坏字节值, 不得修改大小写.

    参数:
        buf: 内存区域字节串.
        patterns: collect_dat_patterns 返回的 pattern 列表.
        key_hits: key_hex_str -> 命中 pattern 数 (原地更新, 取最大值).
        diag: 诊断字典, 含 solved_samples 列表 (原地追加).

    返回:
        本次累积命中的 pattern 总数 (用于日志).
    """
    if not patterns:
        return 0

    # 把所有 pattern 密文拼接为单次 AES 批处理输入, 减少 CCCrypt 调用次数
    batch_ct = b"".join(p["ct"] for p in patterns)
    n_pat = len(patterns)
    matches = 0
    # 在当前 buffer 内去重, 避免同一密钥候选被重复 AES 解密
    seen_local: set[bytes] = set()

    for m in HEX32_IMG_RE.finditer(buf):
        cand = m.group(0)            # 32 字节 hex ASCII 原文
        key = cand[:16]              # 前 16 个 ASCII 字节即真实 AES key
        if key in seen_local:
            continue
        seen_local.add(key)
        # 批量解密: 一次 CCCrypt 调用解出所有 pattern 的第一块
        pt = aes_ecb_decrypt(key, batch_ct)
        if pt is None:
            continue
        hits = 0
        for j in range(n_pat):
            # 检查每个 pattern 对应的解密块是否有合法图像魔数
            if is_image_magic(pt[j * 16:(j + 1) * 16]):
                hits += 1
        if hits > 0:
            # 注意: 对外输出 hex ASCII 原文 (保留大小写), 下游 decrypt_v4 用
            # str(aes_key).encode()[:16] 还原真实 key, lower/再编码都会破坏字节
            key_str = cand.decode("ascii")
            prev = key_hits.get(key_str, 0)
            if hits > prev:
                key_hits[key_str] = hits
            matches += hits
            if hits >= 3 and len(diag["solved_samples"]) < 5:
                diag["solved_samples"].append({
                    "key": key_str, "hits": hits, "of": n_pat,
                })

    return matches


def run_image_key_mode(dat_dir: str, limit_mb: int, log: list, output: dict) -> int:
    """执行 image-key 模式的完整扫描流程.

    流程:
      1. 收集 V4 .dat 文件的 AES 密文 pattern.
      2. 枚举微信进程的可读内存区域.
      3. 逐区域扫描候选密钥, 按命中 pattern 数排序.
      4. 取命中数 >= 2 的密钥为权威密钥; 不足时取最高命中者兜底.
      5. 对每个 pattern 分配第一个能解开的密钥, 输出 {ct_hex: key_str} 映射.

    参数:
        dat_dir: 微信图片缓存目录 (含 V4 .dat 文件).
        limit_mb: 单个内存区域大小上限 (MB).
        log: 日志字符串列表 (原地追加).
        output: 结果字典 (原地写入 keys/primary_keys/diag).

    返回:
        0 = 成功; 1 = 致命错误 (output["error"] 已设置).
    """
    if not HAS_CCCRYPT:
        output["error"] = "CommonCrypto (CCCrypt) 不可用, 无法做 AES 验证"
        return 1
    if not os.path.isdir(dat_dir):
        output["error"] = f"dat-dir 不存在: {dat_dir}"
        return 1

    patterns = collect_dat_patterns(dat_dir)
    log.append(f"collected {len(patterns)} V4 dat patterns from {dat_dir}")
    if not patterns:
        output["error"] = "未在 dat-dir 中找到 V4 格式 .dat 文件"
        return 1

    pids = find_wechat_pids()
    log.append(f"wechat pids: {pids}")
    if not pids:
        output["error"] = "WeChat / Weixin process not running"
        return 1

    # key_hex_str -> 该 key 能解出的 pattern 数量 (越高越可信)
    key_hits: dict[str, int] = {}
    diag = {"solved_samples": [], "regions_scanned": 0, "bytes_scanned": 0}
    started = time.time()

    for pid in pids:
        try:
            task = task_for_pid(pid)
        except Exception as e:
            log.append(f"task_for_pid {pid} failed: {e}")
            continue
        for addr, size in enum_readable_regions(task, max_size=limit_mb * 1024 * 1024):
            buf = read_region(task, addr, size)
            if buf is None:
                continue
            diag["regions_scanned"] += 1
            diag["bytes_scanned"] += len(buf)
            scan_buffer_image(buf, patterns, key_hits, diag)

    elapsed = time.time() - started

    # 取命中数 >= 2 的 key 作为权威密钥; 只命中 1 的多半假阳性, 但保留前 8 个备用
    sorted_keys = sorted(key_hits.items(), key=lambda kv: kv[1], reverse=True)
    primary_keys = [k for k, c in sorted_keys if c >= 2]
    if not primary_keys and sorted_keys:
        primary_keys = [sorted_keys[0][0]]  # 兜底: pattern 太少时也得给一个
    extra_keys = [k for k, c in sorted_keys[:8] if c < 2 and k not in primary_keys]

    # 用 primary_keys 顺序解每个 pattern, 第一个能解开就赋值
    # (kh 是 32-hex ASCII 原文, 真实 key = 其前 16 个 ASCII 字节)
    key_map: dict[str, str] = {}
    for p in patterns:
        for kh in primary_keys + extra_keys:
            pt = aes_ecb_decrypt(kh.encode("ascii")[:16], p["ct"])
            if pt and is_image_magic(pt):
                key_map[p["ct_hex"]] = kh
                break

    log.append(
        f"image-key done in {elapsed:.1f}s, "
        f"unique_keys={len(key_hits)}, primary(>=2)={len(primary_keys)}, "
        f"solved={len(key_map)}/{len(patterns)}, "
        f"regions={diag['regions_scanned']}, bytes={diag['bytes_scanned'] // (1024 * 1024)}MB"
    )
    if sorted_keys[:5]:
        log.append("top5 key hits: " + ", ".join(f"{k[:8]}..={c}" for k, c in sorted_keys[:5]))

    output["keys"] = key_map
    output["primary_keys"] = primary_keys
    output["diag"] = diag
    return 0


# ─── main ─────────────────────────────────────────
def main() -> int:
    """脚本入口: 解析命令行参数, 分发到对应扫描模式, 将结果序列化为 JSON 输出到 stdout.

    返回:
        0 = 成功; 1 = 致命错误.
    """
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["db-key", "image-key"], default="db-key")
    ap.add_argument("--db-dir", default=None)
    ap.add_argument("--dat-dir", default=None)
    ap.add_argument("--limit-mb", type=int, default=500)
    args = ap.parse_args()

    log: list[str] = []
    output = {"keys": {}, "log": log, "error": None, "mode": args.mode}

    if args.mode == "image-key":
        rc = run_image_key_mode(args.dat_dir or "", args.limit_mb, log, output)
        json.dump(output, sys.stdout, ensure_ascii=False)
        return rc

    # db-key 模式 (默认)
    db_dir = args.db_dir
    if not db_dir or not os.path.isdir(db_dir):
        output["error"] = f"db_dir not found: {db_dir}"
        json.dump(output, sys.stdout, ensure_ascii=False)
        return 1

    files, salts = collect_dbs(db_dir)
    log.append(f"collected {len(files)} db, {len(salts)} salts")
    if not salts:
        output["error"] = "no .db files in db_dir"
        json.dump(output, sys.stdout, ensure_ascii=False)
        return 1

    pids = find_wechat_pids()
    log.append(f"wechat pids: {pids}")
    if not pids:
        output["error"] = "WeChat / Weixin process not running"
        json.dump(output, sys.stdout, ensure_ascii=False)
        return 1

    key_map: dict[str, str] = {}
    # remaining 追踪还未找到密钥的 salt, 全部找到后提前退出扫描
    remaining = set(salts.keys())
    total_matches = 0
    diag = {"by_len": {}, "salt_samples": [], "key_samples": [], "match_no_hmac": 0}
    started = time.time()

    for pid in pids:
        try:
            task = task_for_pid(pid)
        except Exception as e:
            log.append(f"task_for_pid {pid} failed: {e}")
            continue
        regions = list(enum_readable_regions(task, max_size=args.limit_mb * 1024 * 1024))
        log.append(f"pid={pid} regions={len(regions)}")
        for addr, size in regions:
            buf = read_region(task, addr, size)
            if buf:
                total_matches += scan_buffer(buf, files, salts, key_map, remaining, diag)
            if not remaining:
                # 所有数据库密钥已全部找到, 无需继续扫描
                break
        if not remaining:
            break

    elapsed = time.time() - started
    log.append(f"done in {elapsed:.1f}s, hits={len(key_map)}/{len(salts)}, hex_matches={total_matches}")
    log.append(f"hex 长度分布: {diag['by_len']}")
    log.append(f"扫到的 96-hex 后32位 sample (前8): {diag['salt_samples']}")
    log.append(f"已知 db salts (前8): {sorted(salts.keys())[:8]}")
    log.append(f"96-hex 长度命中但 HMAC 失败次数: {diag['match_no_hmac']}")
    if diag.get("key_samples"):
        log.append(f"扫到的 64-hex sample (前8): {diag['key_samples'][:8]}")
    output["keys"] = key_map
    output["diag"] = diag
    json.dump(output, sys.stdout, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
