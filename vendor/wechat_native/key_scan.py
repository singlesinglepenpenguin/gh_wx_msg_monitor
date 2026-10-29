"""跨平台通用密钥扫描工具.

职责:
  * collect_db_files: 枚举 db_storage 目录下的 .db 文件, 读取 SQLCipher page1
    并提取前 16 字节 salt, 构建 salt_hex -> [rel_path, ...] 映射.
  * verify_enc_key: 用 HMAC-SHA512 校验候选 enc_key 是否能正确解开对应 page1.
  * scan_buffer: 在一段二进制内存块中正则匹配 x'<hex>' 模式, 对命中结果做
    HMAC 验证, 通过则写入 key_map 并从 remaining_salts 移除.

平台差异:
  本模块不依赖任何 macOS / Windows 平台 API, 可在任何平台 import.
  真正的进程内存获取由 scanner_mac.py / scanner_win.py 负责, 得到字节块后
  传入本模块的 scan_buffer 统一处理.

被谁调用:
  * scanner_win.py: 直接 import collect_db_files, scan_buffer 在进程循环中调用.
  * _mac_helper.py: 以相同方式调用 (helper 以独立 python3 进程运行, 重新 import).
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import struct
from typing import Callable, Iterable

from .crypto import PAGE_SZ, SALT_SZ, derive_key

KEY_SZ: int = 32          # enc_key 长度 (字节), SQLCipher 4 默认 AES-256
HMAC_LEN: int = 64       # SHA-512 输出长度 (字节)
RESERVE_SZ: int = 80     # SQLCipher page1 末尾预留区: IV(16) + HMAC(64)

# 微信 SQLCipher 密钥在进程堆中以 x'<hex>' 格式存储, 与 sqlite3 PRAGMA key 语法一致.
# 匹配长度 64~192: 64=单纯 enc_key, 96=enc_key+salt, 更长的包含 IV/HMAC 等附加字段.
HEX_PATTERN: re.Pattern[bytes] = re.compile(rb"x'([0-9a-fA-F]{64,192})'")


# ─── 1. HMAC 验证 ─────────────────────────────────────────
def verify_enc_key(enc_key: bytes, db_page1: bytes) -> bool:
    """通过 page1 末尾的 64 字节 HMAC-SHA512 验证候选密钥是否正确.

    SQLCipher 4 的 page1 结构:
      [0:16]          salt (明文存储, 用于 KDF)
      [16:PAGE_SZ-64] 加密数据
      [PAGE_SZ-64:]   HMAC-SHA512 (用于完整性校验)

    验证步骤:
      mac_salt = page1[:16] xor 0x3A     (防止与 KDF salt 复用同一字节序列)
      mac_key  = PBKDF2-SHA512(enc_key, mac_salt, 2 iter, dklen=32)
      data     = page1[16 : PAGE_SZ-64] + struct.pack('<I', 1)   (page number = 1)
      hmac_sha512(mac_key, data) 应等于 page1[PAGE_SZ-64 : PAGE_SZ]

    参数:
        enc_key:  候选加密密钥 (32 字节).
        db_page1: 数据库文件第一页原始字节 (至少 PAGE_SZ 字节).

    返回:
        True 表示 HMAC 验证通过, 即 enc_key 是该数据库的正确密钥.
    """
    if len(db_page1) < PAGE_SZ:
        return False
    salt = db_page1[:SALT_SZ]
    # 0x3A 异或是 SQLCipher 规范要求, 防止 KDF salt 和 MAC salt 相同
    mac_salt = bytes(b ^ 0x3A for b in salt)
    mac_key = hashlib.pbkdf2_hmac("sha512", enc_key, mac_salt, 2, dklen=KEY_SZ)
    hmac_data = db_page1[SALT_SZ: PAGE_SZ - HMAC_LEN]
    stored = db_page1[PAGE_SZ - HMAC_LEN: PAGE_SZ]
    h = hmac.new(mac_key, hmac_data, hashlib.sha512)
    # page number (小端 uint32) 追加到 HMAC 数据末尾, page1 固定为 1
    h.update(struct.pack("<I", 1))
    return h.digest() == stored


# ─── 2. DB 枚举 ───────────────────────────────────────────
class DbFile:
    """单个 SQLCipher 数据库文件的元信息.

    属性:
        rel:      相对于 db_dir 的路径 (用于日志展示).
        abs:      绝对路径.
        size:     文件大小 (字节).
        salt_hex: page1 前 16 字节的 hex 字符串, 作为唯一标识.
        page1:    文件第一页原始字节 (PAGE_SZ 字节), 用于 HMAC 验证.
    """
    __slots__ = ("rel", "abs", "size", "salt_hex", "page1")

    def __init__(self, rel: str, abs_path: str, size: int, salt_hex: str, page1: bytes) -> None:
        """内部辅助函数: 处理   init   中间步骤，供微信本地数据流程复用。"""
        self.rel = rel
        self.abs = abs_path
        self.size = size
        self.salt_hex = salt_hex
        self.page1 = page1


def collect_db_files(db_dir: str) -> tuple[list[DbFile], dict[str, list[str]]]:
    """递归扫描 db_dir 下的所有 .db 文件, 读取 page1 并提取 salt.

    过滤规则:
      * 跳过 WAL/SHM 辅助文件 (以 -wal / -shm 结尾).
      * 跳过文件大小不足一页 (PAGE_SZ) 的空/损坏文件.
      * 权限不足无法读取时静默跳过 (OSError).

    返回:
        (files, salt_map):
          - files:    DbFile 对象列表.
          - salt_map: {salt_hex: [rel_path, ...]} 同一 salt 可能对应多个 db 文件
                      (微信多账号场景或备份文件).
    """
    files: list[DbFile] = []
    salt_map: dict[str, list[str]] = {}
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
            salt = page1[:SALT_SZ].hex()
            rel = os.path.relpath(full, db_dir)
            files.append(DbFile(rel, full, size, salt, page1))
            # 同一 salt 可能对应多个 db (多账号), 全部记录
            salt_map.setdefault(salt, []).append(rel)
    return files, salt_map


# ─── 3. 内存扫描 hex 模式 ─────────────────────────────────
def scan_buffer(
    buf: bytes,
    db_files: Iterable[DbFile],
    salt_map: dict[str, list[str]],
    key_map: dict[str, str],
    remaining_salts: set[str],
    on_found: Callable[[str, str, list[str]], None] | None = None,
) -> int:
    """在内存块 buf 中扫描 hex 模式, 命中并 HMAC 验证后写入 key_map.

    匹配模式为 x'<hex>', hex 长度 64~192. 根据长度采用不同的解析策略:
      * 长度 == 64:  纯 enc_key hex, 需与所有 remaining_salts 逐一做 HMAC 验证.
      * 长度 == 96:  enc_key(前 64) + salt(后 32), 可直接定位 db 文件.
      * 长度 > 96 且为偶数: 头 64 = enc_key, 尾 32 = salt (中间字段忽略).

    参数:
        buf:             一段进程内存字节块 (从 ReadProcessMemory / mach_vm_read 读取).
        db_files:        collect_db_files 返回的 DbFile 列表.
        salt_map:        {salt_hex: [rel_path, ...]} 映射.
        key_map:         输出字典, 函数就地写入命中的 {salt_hex: enc_key_hex}.
        remaining_salts: 尚未找到 enc_key 的 salt 集合, 命中后就地移除.
        on_found:        可选回调, 每找到一对 (salt, key) 时调用.

    返回:
        本次在 buf 中正则匹配到的 hex 模式总数 (不论是否通过 HMAC 验证).
    """
    db_list = list(db_files)
    matches = 0
    for m in HEX_PATTERN.finditer(buf):
        hex_str = m.group(1).decode()
        matches += 1
        n = len(hex_str)

        if n == 96:
            # 96 字符 = 48 字节: enc_key(32B) + salt(16B) 各自 hex 编码
            enc_key_hex = hex_str[:64]
            salt_hex = hex_str[64:]
            if salt_hex not in remaining_salts:
                continue
            enc_key = bytes.fromhex(enc_key_hex)
            for db in db_list:
                if db.salt_hex == salt_hex and verify_enc_key(enc_key, db.page1):
                    key_map[salt_hex] = enc_key_hex
                    remaining_salts.discard(salt_hex)
                    if on_found:
                        on_found(salt_hex, enc_key_hex, salt_map[salt_hex])
                    break

        elif n == 64:
            # 64 字符 = 32 字节: 纯 enc_key, 不含 salt 信息, 须与全部 remaining salts 验证
            if not remaining_salts:
                continue
            enc_key = bytes.fromhex(hex_str)
            for db in db_list:
                if db.salt_hex in remaining_salts and verify_enc_key(enc_key, db.page1):
                    key_map[db.salt_hex] = hex_str
                    remaining_salts.discard(db.salt_hex)
                    if on_found:
                        on_found(db.salt_hex, hex_str, salt_map[db.salt_hex])
                    break

        elif n > 96 and n % 2 == 0:
            # 超长 hex: 取头 64 字符作 enc_key, 尾 32 字符作 salt, 中间字段忽略
            enc_key_hex = hex_str[:64]
            salt_hex = hex_str[-32:]
            if salt_hex not in remaining_salts:
                continue
            enc_key = bytes.fromhex(enc_key_hex)
            for db in db_list:
                if db.salt_hex == salt_hex and verify_enc_key(enc_key, db.page1):
                    key_map[salt_hex] = enc_key_hex
                    remaining_salts.discard(salt_hex)
                    if on_found:
                        on_found(salt_hex, enc_key_hex, salt_map[salt_hex])
                    break

    return matches


# ─── 3b. 内存扫描 raw enc_key (微信 4.1+) ─────────────────
def scan_buffer_raw(
    buf: bytes,
    db_files: Iterable[DbFile],
    key_map: dict[str, str],
    remaining_salts: set[str],
    on_found: Callable[[str, str, list[str]], None] | None = None,
    step: int = 8,
) -> int:
    """微信 4.1+: 把内存块按 step 字节对齐滑窗, 每 32 字节当 raw enc_key 候选, verify_enc_key 校验。

    4.1+ 不再把 key 以 x'<hex>' 文本写进内存 (scan_buffer 0 命中), 但运行时 SQLCipher
    codec 仍持有 raw 32 字节 key 用于解密已打开的库 —— 直接扫 raw 字节即可命中, 无需口令/断点。

    便宜预筛: 跳过前 8 字节全零的窗口 (真 key 高熵, 前 8 字节全零概率 ~1/256^8, 几乎不漏;
    内存大量全零区借此跳过, 大幅减少 HMAC 次数)。step 默认 8 (key 多 8 字节对齐), 漏检可降到 1。

    返回实际做过 verify 的候选数 (用于诊断/进度)。
    """
    targets = [db for db in db_files if db.salt_hex in remaining_salts]
    if not targets or len(buf) < KEY_SZ:
        return 0
    tried = 0
    last = len(buf) - KEY_SZ
    zero8 = b"\x00" * 8
    off = 0
    while off <= last:
        cand = buf[off:off + KEY_SZ]
        if cand[:8] != zero8:
            for db in targets:
                if db.salt_hex in remaining_salts and verify_enc_key(cand, db.page1):
                    key_map[db.salt_hex] = cand.hex()
                    remaining_salts.discard(db.salt_hex)
                    if on_found:
                        on_found(db.salt_hex, cand.hex(), [db.rel])
                    targets = [d for d in targets if d.salt_hex in remaining_salts]
                    break
            tried += 1
            if not remaining_salts:
                break
        off += step
    return tried


# ─── 4. 口令派生 (微信 4.1.x) ─────────────────────────────
def keys_from_passphrase(passphrase: bytes, db_dir: str) -> dict[str, str]:
    """微信 4.1.x: 用抓到的口令为 db_dir 下每个库派生并校验 enc_key.

    4.1.x 内存不再缓存 enc_key (旧 scan_buffer 0 命中), 只剩口令。对每个 .db 的
    page1 salt 调 derive_key(passphrase, salt) 得候选 enc_key, 再用 verify_enc_key
    做 SQLCipher4 HMAC 校验; 通过才记入结果。产出格式与 scan_buffer 完全一致,
    下游 (messages.decrypt_all_dbs 等) 无需区分来源。

    参数:
        passphrase: 从微信进程抓取的口令 (bytes, 通常 32 字节)。
        db_dir: 微信数据库目录 (db_storage)。

    返回:
        {salt_hex: enc_key_hex} (每个均通过 HMAC 校验); 口令错或无库时返回 {}。
    """
    files, _salt_map = collect_db_files(db_dir)
    key_map: dict[str, str] = {}
    for db in files:
        if db.salt_hex in key_map:
            continue  # 同一 salt 只需派生一次
        try:
            salt = bytes.fromhex(db.salt_hex)
        except ValueError:
            continue
        enc_key = derive_key(passphrase, salt)
        if verify_enc_key(enc_key, db.page1):
            key_map[db.salt_hex] = enc_key.hex()
    return key_map


# ─── 5. enc_key 候选认领 (微信 4.1.x, lldb 现场抓 AES key) ──
def keys_from_enc_candidates(candidates: Iterable[bytes | str], db_dir: str) -> dict[str, str]:
    """微信 4.1.x: 给一组候选 enc_key (lldb 从 CCCryptorCreate 现场抓的 AES key),
    用每库 page1 的 SQLCipher HMAC 逐一认领, 输出 {salt_hex: enc_key_hex}。

    与 keys_from_passphrase 互补: 口令仅在开新库 PBKDF2 时出现, 而 enc_key 在运行中读
    任意**已打开**的库时即可抓到 (无需重启/重登)。候选未必都属于本目录的库 (可能混入
    TLS 等无关 AES-256 key), 一律用 verify_enc_key 过滤, 不通过即丢弃。产出格式与
    scan_buffer / keys_from_passphrase 完全一致, 下游无需区分来源。

    参数:
        candidates: 候选 enc_key, 元素为 32 字节 bytes 或 64 hex 字符串。
        db_dir: 微信数据库目录 (db_storage)。

    返回:
        {salt_hex: enc_key_hex} (每个均通过 HMAC 校验); 全不匹配时返回 {}。
    """
    cand: list[bytes] = []
    seen: set[bytes] = set()
    for c in candidates:
        if isinstance(c, str):
            try:
                c = bytes.fromhex(c.strip())
            except ValueError:
                continue
        b = bytes(c) if isinstance(c, (bytes, bytearray)) else None
        if b and len(b) == KEY_SZ and b not in seen:
            seen.add(b)
            cand.append(b)
    files, _salt_map = collect_db_files(db_dir)
    key_map: dict[str, str] = {}
    for db in files:
        if db.salt_hex in key_map:
            continue  # 同一 salt 只需认领一次
        for enc_key in cand:
            if verify_enc_key(enc_key, db.page1):
                key_map[db.salt_hex] = enc_key.hex()
                break
    return key_map
