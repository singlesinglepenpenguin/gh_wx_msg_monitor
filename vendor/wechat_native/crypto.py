"""SQLCipher 4 解密 -- 纯 Python 实现.

职责:
  将微信使用的 SQLCipher 4 加密数据库解密为标准 SQLite3 文件,
  并支持把 WAL (Write-Ahead Logging) 中未 checkpoint 的变更回填到主库.

调用方:
  wechat_native/wechat.py 在取得 enc_key 后调用 full_decrypt / apply_wal,
  生成可供 sqlite3 直接打开的明文数据库副本.

SQLCipher 4 协议参数 (与微信 macOS 版本对齐):
  PAGE_SZ  = 4096 字节 (固定页大小)
  SALT_SZ  = 16   字节 (第一页开头, 用于 KDF 盐值)
  RESERVE  = 80   字节 (每页尾部保留区 = IV 16 + HMAC-SHA512 64)
  KDF      : PBKDF2-SHA512, 256000 次迭代, dklen=32 (enc_key 推导)
             mac_key 另行推导: PBKDF2-SHA512(enc_key, salt^0x3A, iter=2, dklen=32)
  加密算法 : AES-256-CBC, IV 取自每页尾部 RESERVE 区的前 16 字节
  第一页特殊处理: 前 16 字节是 SALT (不加密), 解密后用标准 SQLite 文件头替换

外部 API:
  decrypt_page(enc_key, page_data, pgno) -> bytes
  full_decrypt(src_db, dst_db, enc_key)  -> int  (返回 page 数)
  apply_wal(wal_path, dst_db, enc_key)   -> int  (返回回填 page 数)
"""

from __future__ import annotations

import hashlib
import os
import struct
from typing import Final

from Crypto.Cipher import AES

# SQLCipher 页大小 (字节), 固定为 4096
PAGE_SZ: Final = 4096
# 第一页 salt 长度 (字节), 位于页首
SALT_SZ: Final = 16
# 每页尾部保留区大小: IV(16) + HMAC-SHA512(64)
RESERVE_SZ: Final = 80          # IV(16) + HMAC-SHA512(64)
# IV 在页内的起始偏移 = PAGE_SZ - RESERVE_SZ
IV_OFFSET: Final = PAGE_SZ - RESERVE_SZ
# 解密后第一页需要用标准 SQLite 文件头替换原来的 salt 区
SQLITE_HDR: Final = b"SQLite format 3\x00"

# WAL 文件头大小 (字节)
WAL_HEADER_SZ: Final = 32
# WAL 每个 frame 的头部大小 (字节)
WAL_FRAME_HEADER_SZ: Final = 24

# SQLCipher 4 KDF 迭代次数 (PBKDF2-SHA512); 微信沿用默认 256000。
# 微信 4.1.x 起内存不再缓存 enc_key, 改由口令 (passphrase) 现场派生时用此参数。
KDF_ITER: Final = 256000


def derive_key(passphrase: bytes, salt: bytes) -> bytes:
    """由口令 (passphrase) + 数据库 salt 派生 32 字节 enc_key (微信 4.1.x).

    4.1.x 不再把 enc_key 缓存在内存, 只保留口令; SQLCipher 用
    PBKDF2-SHA512(passphrase, salt, 256000, dklen=32) 现场派生每个库的 enc_key。
    4.0.x 直接从内存拿到 enc_key, 不走本函数。

    参数:
        passphrase: 从微信进程抓取的口令 (通常 32 字节)。
        salt: 目标数据库 page1 前 16 字节。

    返回:
        32 字节 enc_key, 可直接传给 decrypt_page / full_decrypt。
    """
    return hashlib.pbkdf2_hmac("sha512", passphrase, salt, KDF_ITER, dklen=32)


def decrypt_page(enc_key: bytes, page_data: bytes, pgno: int) -> bytes:
    """解密单个 SQLCipher 页, 返回与原页同大小 (PAGE_SZ) 的明文.

    第一页 (pgno=1) 布局:
      [0:16]          = SALT (不参与加密, 解密后替换为 SQLITE_HDR)
      [16:IV_OFFSET]  = AES-CBC 密文
      [IV_OFFSET:IV_OFFSET+16] = IV (在 RESERVE 区开头)
      [IV_OFFSET+16:] = HMAC (不解密, 输出填零)

    其他页布局:
      [0:IV_OFFSET]   = AES-CBC 密文 (整页加密)
      [IV_OFFSET:IV_OFFSET+16] = IV
      [IV_OFFSET+16:] = HMAC (不解密, 输出填零)

    参数:
        enc_key: AES-256 密钥 (32 字节), 由 PBKDF2-SHA512 推导.
        page_data: 原始加密页字节, 长度必须等于 PAGE_SZ.
        pgno: 页号 (从 1 开始), 用于区分第一页的特殊处理.

    返回:
        PAGE_SZ 字节的明文页; 尾部 RESERVE_SZ 字节填零 (HMAC 不输出).

    异常:
        ValueError: page_data 长度不等于 PAGE_SZ.
    """
    if len(page_data) != PAGE_SZ:
        raise ValueError(f"page size != {PAGE_SZ}: {len(page_data)}")
    # IV 固定在 RESERVE 区的起始位置 (每页均相同偏移)
    iv = page_data[IV_OFFSET: IV_OFFSET + 16]
    if pgno == 1:
        # 第一页: 跳过 SALT_SZ 字节的 salt, 只解密 salt 之后到 IV 之前的内容
        encrypted = page_data[SALT_SZ: IV_OFFSET]
        plain = AES.new(enc_key, AES.MODE_CBC, iv).decrypt(encrypted)
        # 解密后用标准 SQLite 文件头替换 salt 区, 使输出可被 sqlite3 识别
        return SQLITE_HDR + plain + b"\x00" * RESERVE_SZ
    # 其他页: 整页 (到 IV_OFFSET) 参与加密
    encrypted = page_data[:IV_OFFSET]
    plain = AES.new(enc_key, AES.MODE_CBC, iv).decrypt(encrypted)
    # 尾部 RESERVE_SZ 字节 (IV + HMAC) 在明文数据库中填零
    return plain + b"\x00" * RESERVE_SZ


def full_decrypt(src_db: str, dst_db: str, enc_key: bytes) -> int:
    """整库解密: 逐页 AES 解密后写到 dst_db.

    按页顺序读取 src_db, 逐页调用 decrypt_page, 结果写入 dst_db.
    dst_db 所在目录不存在时自动创建.

    参数:
        src_db: 加密数据库文件路径.
        dst_db: 输出明文数据库文件路径.
        enc_key: AES-256 密钥 (32 字节).

    返回:
        成功解密的页数. 如果文件末尾有不足一页的数据则静默忽略.

    副作用: 在磁盘创建或覆盖 dst_db 文件.
    """
    file_size = os.path.getsize(src_db)
    total_pages = file_size // PAGE_SZ
    # 确保目标目录存在 ("." 处理 dst_db 无父目录的情况)
    os.makedirs(os.path.dirname(dst_db) or ".", exist_ok=True)
    with open(src_db, "rb") as fin, open(dst_db, "wb") as fout:
        for pgno in range(1, total_pages + 1):
            page = fin.read(PAGE_SZ)
            if len(page) < PAGE_SZ:
                # 文件末尾不足一页, 停止 (不应发生于正常数据库)
                break
            fout.write(decrypt_page(enc_key, page, pgno))
    return total_pages


def apply_wal(wal_path: str, dst_db: str, enc_key: bytes) -> int:
    """把 WAL 中尚未 checkpoint 的 frame 解密后回填到 dst_db.

    WAL (Write-Ahead Log) 文件格式关键字段:
      WAL 头 (32 字节):
        [16:20] = salt1 (大端序 uint32)
        [20:24] = salt2 (大端序 uint32)
      Frame 头 (24 字节, 每个 frame 的起始):
        [0:4]   = pgno  (大端序 uint32, 该 frame 对应的数据库页号)
        [8:12]  = fSalt1 (大端序 uint32, 必须与 WAL header salt1 一致才有效)
        [12:16] = fSalt2 (大端序 uint32, 同上)
        frame 数据: 紧随 frame 头之后的 PAGE_SZ 字节 (加密页内容)

    有效 frame 判定:
      fSalt1 == wal_salt1 且 fSalt2 == wal_salt2, 且 pgno 在合理范围 (1~1,000,000).
    无效 frame (salt 不匹配) 表示该 frame 属于旧的 checkpoint 轮次, 跳过.

    参数:
        wal_path: WAL 文件路径 (通常为 <db>.db-wal).
        dst_db: 已由 full_decrypt 生成的明文数据库路径, 以 r+b 模式就地修改.
        enc_key: AES-256 密钥 (32 字节).

    返回:
        成功回填的 frame 数; WAL 不存在或为空时返回 0.

    副作用: 就地修改 dst_db 文件 (按页号 seek 写入).
    """
    if not os.path.exists(wal_path):
        return 0
    wal_size = os.path.getsize(wal_path)
    if wal_size <= WAL_HEADER_SZ:
        # WAL 文件只有头部或为空, 无有效 frame
        return 0
    patched = 0
    with open(wal_path, "rb") as wf, open(dst_db, "r+b") as df:
        wal_hdr = wf.read(WAL_HEADER_SZ)
        # 读取 WAL 全局 salt, 用于验证 frame 是否属于当前 checkpoint 轮次
        wal_salt1 = struct.unpack(">I", wal_hdr[16:20])[0]
        wal_salt2 = struct.unpack(">I", wal_hdr[20:24])[0]
        frame_size = WAL_FRAME_HEADER_SZ + PAGE_SZ
        while wf.tell() + frame_size <= wal_size:
            fh = wf.read(WAL_FRAME_HEADER_SZ)
            if len(fh) < WAL_FRAME_HEADER_SZ:
                break
            # 解析 frame 头: 页号和 salt
            pgno = struct.unpack(">I", fh[0:4])[0]
            fsalt1 = struct.unpack(">I", fh[8:12])[0]
            fsalt2 = struct.unpack(">I", fh[12:16])[0]
            # 读取 frame 的加密页数据
            ep = wf.read(PAGE_SZ)
            if len(ep) < PAGE_SZ:
                break
            # 跳过页号无效的 frame (0 或超过百万页视为异常)
            if pgno == 0 or pgno > 1_000_000:
                continue
            # salt 不匹配表示旧轮次的 frame, 跳过
            if fsalt1 != wal_salt1 or fsalt2 != wal_salt2:
                continue
            # 解密 frame 并回填到目标数据库对应页位置
            plain = decrypt_page(enc_key, ep, pgno)
            df.seek((pgno - 1) * PAGE_SZ)
            df.write(plain)
            patched += 1
    return patched
