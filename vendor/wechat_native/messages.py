"""微信聊天记录数据库读取与查询模块。

职责:
  - 解密微信 macOS 4.x 的 SQLCipher 数据库 (message_*.db / biz_message_*.db 等)
  - 构建联系人 username/hash/显示名三级索引, 支持昵称/备注模糊反查
  - 对外暴露三个主要入口:
      decrypt_all_dbs(db_dir, key_map, cache_dir)  -- 增量批量解密到本地缓存目录
      list_sessions(cache_dir)                     -- 列举所有会话 (表名 hash + 消息数 + 最后时间)
      search_messages(cache_dir, ...)              -- 多条件全文检索, 返回结构化消息列表

解密机制:
  微信 macOS 4.x 对数据库使用 SQLCipher (AES-256-CBC, 16 字节 salt 置于文件头).
  key_map 由调用方 (api/wechat.py 的 /api/wx/* 路由) 从内存/Keychain 中提取后传入,
  格式为 {salt_hex: enc_key_hex}. 本模块调用 wechat_native.crypto.full_decrypt 完成
  页级 AES 解密, 再通过 apply_wal 将 WAL 日志合并进解密后的副本.
  为避免每次请求都重解密 (单库解密需 1-3 秒), 通过源文件 mtime+size 指纹实现增量跳过.

表结构约定:
  每个 message_*.db 内按聊天对象分表, 表名为 Msg_<md5(username)>, 共 32 位十六进制.
  消息内容字段 message_content: 文本类型直接为 UTF-8 字符串; 非文本类型为 zstd 压缩的
  XML/protobuf BLOB. local_type 低 16 位表示消息类型 (文本=1, 图片=3, 链接=49 等).

被谁调用:
  api/wechat.py -- 通过 /api/wx/sessions、/api/wx/search 路由调用本模块.
  不直接被前端访问, 前端经 Vite proxy -> FastAPI -> 本模块.

简化点 (v1):
  * 只走 message_*.db (聊天历史), 不解析 contact / favorite / media
  * 表名约定 Msg_<md5(username_hex)>; chat_name 直接当 username 走 md5 (后续可加 contact 名->wxid 反查)
  * 不解 media (图片 / 语音), 只取文本 / link 内容
  * Type 映射只覆盖最常用 (text=1, image=3, voice=34, video=43, link=49, sticker=47, system=10000)
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import sys
import tempfile
import threading
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

try:
    from wechat_log import wlog
except Exception:  # pragma: no cover
    def wlog(level: str, message: str) -> None:
        """处理 wlog 业务逻辑，保持输入、输出与异常语义清晰。"""
        print(f"[wechat:{level}] {message}", file=sys.stderr)

from .crypto import full_decrypt, apply_wal


# 匹配聊天记录数据库文件名, 如 message_0.db / message_42.db
_MSG_DB_RE = re.compile(r"message_\d+\.db$", re.IGNORECASE)
# 白名单: 只允许 Msg_ + 32 位小写 hex, 防止 SQL 注入式表名
_SAFE_TABLE_RE = re.compile(r"^Msg_[0-9a-f]{32}$")

# 微信 4.x macOS 的 local_type 值. 真实 type 在低 16 位, 高位是 source/origin flag
MSG_TYPE_NAMES: dict[int, str] = {
    1: "文本",
    3: "图片",
    34: "语音",
    37: "好友申请",
    42: "名片",
    43: "视频",
    47: "表情",
    48: "位置",
    49: "链接/文件",
    50: "通话",
    51: "小程序",
    62: "小视频",
    10000: "系统",
    10002: "撤回",
}


def _normalize_type(raw_type: int) -> int:
    """规范化 local_type, 提取真正的消息类型 ID。

    微信 4.x 把 source flag 编进 local_type 高位 (例如 21474836529 = 5<<32 | 1).
    取低 16 位才是真正的消息类型.

    Args:
        raw_type: 数据库中读出的原始 local_type 值。

    Returns:
        规范化后的消息类型 ID (低 16 位), 若仍不在已知字典中则返回低 32 位。
    """
    if raw_type in MSG_TYPE_NAMES:
        return raw_type
    low = raw_type & 0xFFFF
    if low in MSG_TYPE_NAMES:
        return low
    # 兜底取低 32 位
    return raw_type & 0xFFFFFFFF

# zstd 压缩数据的魔数 (4 字节), 用于判断 BLOB 是否需要先解压
_ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
# zstd 解压上下文按线程隔离: ZstdDecompressor 实例非线程安全, FastAPI 多请求
# 并发进入本模块时各请求线程必须各持一份 (延迟初始化复用)
_zstd_tls = threading.local()


def _zdec():
    """获取 (或初始化) 当前线程的 zstd 解压器, 延迟导入 zstandard 包。"""
    dctx = getattr(_zstd_tls, "dctx", None)
    if dctx is None:
        import zstandard as zstd
        dctx = zstd.ZstdDecompressor()
        _zstd_tls.dctx = dctx
    return dctx


def _decompress_content(blob) -> str:
    """将 message_content BLOB 解压并解码为字符串。

    微信 4.x 的 message_content 对非文本消息 (图片/链接/卡片等) 是 zstd 压缩的
    二进制 BLOB, 解压后通常是 UTF-8 编码的 XML 字符串.

    Args:
        blob: 数据库 BLOB 字段值, 可为 None / str / bytes / bytearray。

    Returns:
        解码并初步清洗后的字符串; 无法解码时返回前 200 字节的 hex 表示。
    """
    if blob is None:
        return ""
    if isinstance(blob, str):
        return blob
    if not isinstance(blob, (bytes, bytearray)):
        return str(blob)
    if not blob:
        return ""
    data = bytes(blob)
    if data.startswith(_ZSTD_MAGIC):
        try:
            data = _zdec().decompress(data)
        except Exception:
            # 解压失败时返回十六进制截断, 便于调试
            return data[:200].hex() + ("..." if len(data) > 200 else "")
    # 解压后可能是 UTF-8 文本 / XML / protobuf
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return data[:200].hex() + ("..." if len(data) > 200 else "")
    return _strip_xml(text)


_XML_TAG_RE = re.compile(r"<[^>]+>")


def _strip_xml(text: str) -> str:
    """剥去 XML 标签, 提取纯文本摘要。

    微信系统消息/卡片消息常用 XML 包裹. 若文本看起来不像 XML 则原样返回;
    若是 XML 则替换所有标签为空格并折叠多余空白.
    """
    s = text.strip()
    if not (s.startswith("<") and s.endswith(">")):
        return s
    inner = _XML_TAG_RE.sub(" ", s)
    inner = re.sub(r"\s+", " ", inner).strip()
    return inner or s[:200]


def _attr(xml: str, attr: str) -> str:
    """从 XML 字符串中提取指定属性值, 找不到返回空字符串。"""
    m = re.search(rf'{attr}\s*=\s*"([^"]*)"', xml)
    return m.group(1) if m else ""


def _tag_text(xml: str, tag: str) -> str:
    """从 XML 字符串中提取指定标签的文本内容, 兼容 CDATA 包裹。"""
    m = re.search(rf"<{tag}[^>]*>([^<]*)</{tag}>", xml, re.DOTALL)
    if m:
        return m.group(1).strip()
    # CDATA 格式: <tag><![CDATA[...]]></tag>
    m = re.search(rf"<{tag}[^>]*><!\[CDATA\[(.*?)\]\]></{tag}>", xml, re.DOTALL)
    return m.group(1).strip() if m else ""


def _human_size(n: str | int) -> str:
    """将字节数转为可读字符串 (B / KB / MB / GB)。"""
    try:
        size = int(n)
    except (TypeError, ValueError):
        return str(n)
    if size < 1024:
        return f"{size}B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f}KB"
    if size < 1024 * 1024 * 1024:
        return f"{size / 1024 / 1024:.1f}MB"
    return f"{size / 1024 / 1024 / 1024:.2f}GB"


def _format_image(xml: str) -> str:
    """从图片消息 XML 中提取尺寸信息并返回可读摘要。"""
    # 优先取中等质量图尺寸, 其次缩略图
    w = _attr(xml, "cdnmidwidth") or _attr(xml, "cdnthumbwidth")
    h = _attr(xml, "cdnmidheight") or _attr(xml, "cdnthumbheight")
    if w and h:
        return f"[图片] {w}×{h}"
    return "[图片]"


def _format_video(xml: str) -> str:
    """从视频消息 XML 中提取时长和文件大小并返回可读摘要。"""
    sec = _attr(xml, "playlength")
    length = _attr(xml, "length")
    parts = []
    if sec:
        parts.append(f"{sec}秒")
    if length:
        parts.append(_human_size(length))
    return "[视频] " + " ".join(parts) if parts else "[视频]"


def _format_voice(xml: str) -> str:
    """从语音消息 XML 中提取时长 (毫秒->秒) 并返回可读摘要。"""
    ms = _attr(xml, "voicelength")
    if ms:
        try:
            return f"[语音] {int(ms) / 1000:.1f}秒"
        except (TypeError, ValueError):
            pass
    return "[语音]"


def _format_sticker(xml: str) -> str:
    """表情贴纸消息固定返回占位符。"""
    return "[表情]"


def _format_card(xml: str) -> str:
    """从名片消息 XML 中提取联系人昵称并返回可读摘要。"""
    # 优先 nickname, 其次 fullpy (拼音全拼), 最后 alias (微信号)
    nick = _attr(xml, "nickname") or _attr(xml, "fullpy") or _attr(xml, "alias")
    return f"[名片] {nick}" if nick else "[名片]"


def _format_appmsg(xml: str) -> str:
    """格式化 type=49 的 appmsg 子类型消息。

    type=49 下有多种子类型, 通过内层 <type> 标签区分:
      5=链接, 6=文件, 33/36=小程序, 51=视频号, 57=引用消息, 87=群公告, 2000=转账.
    """
    title = _tag_text(xml, "title") or ""
    des = _tag_text(xml, "des") or ""
    inner_type = _tag_text(xml, "type")
    appname = _tag_text(xml, "appname") or _tag_text(xml, "sourcedisplayname") or ""

    if inner_type == "6":
        size = _tag_text(xml, "totallen")
        return f"[文件] {title}" + (f" ({_human_size(size)})" if size else "")
    if inner_type == "5":
        return f"[链接] {title}" + (f" / {appname}" if appname else "")
    if inner_type in ("33", "36"):
        return f"[小程序] {title}"
    if inner_type == "51":
        return f"[视频号] {title}"
    if inner_type == "87":
        return f"[群公告] {title}"
    if inner_type == "2000":
        return f"[转账] {title}"
    if inner_type == "57":
        # 引用消息 (quote / refer)
        refer = _tag_text(xml, "refermsg") or des
        return f"[引用] {title} ← {refer[:80]}" if refer else f"[引用] {title}"
    text = title or des
    return f"[appmsg type={inner_type}] {text}".strip() if text else f"[appmsg type={inner_type}]"


def _format_system(xml: str) -> str:
    """从系统消息 XML (type=10000) 中提取可读文本, 截断至 300 字符。"""
    s = _strip_xml(xml)
    return s[:300] if s else "[系统消息]"


def format_content(local_type: int, raw) -> str:
    """根据消息类型将原始字段值转为可读字符串。

    Args:
        local_type: 规范化后的消息类型 ID (已经过 _normalize_type 处理)。
        raw: message_content / compress_content / source 字段原始值, 可能是:
            * str  -- 普通好友库 (message_*.db) 的文本直接存 str
            * bytes (zstd magic 开头) -- 公众号库 (biz_message_*.db) 的文本 / 其他类型的 XML BLOB
            * bytes (无 magic) -- 罕见, 按 utf-8 二进制处理

    Returns:
        格式化后的可读摘要字符串.
    """
    # 文本消息 (type=1): 普通好友是 str, 公众号是 zstd 压缩 BLOB
    if local_type == 1:
        if isinstance(raw, str):
            return raw
        if isinstance(raw, (bytes, bytearray)):
            data = bytes(raw)
            if data[:4] == _ZSTD_MAGIC:
                try:
                    return _zdec().decompress(data).decode("utf-8", errors="replace")
                except Exception:
                    return ""
            try:
                return data.decode("utf-8", errors="strict")
            except UnicodeDecodeError:
                # 可能是 protobuf 等二进制, 不显示
                return ""
        return str(raw or "")
    # 其他: 先解压
    text = _decompress_content(raw) if not isinstance(raw, str) else raw
    if not text:
        return ""
    handlers: dict[int, callable] = {
        3: _format_image,
        34: _format_voice,
        43: _format_video,
        47: _format_sticker,
        42: _format_card,
        49: _format_appmsg,
        10000: _format_system,
    }
    f = handlers.get(local_type)
    if f:
        try:
            return f(text)
        except Exception:
            return _strip_xml(text)
    if local_type == 50:
        return "[语音/视频通话]"
    if local_type == 62:
        return "[小视频]"
    return _strip_xml(text)


# ─── sender id (real_sender_id) -> 显示名 反查 ───────────
# 模块级缓存: 用 contact.db 的 mtime/size 指纹做失效检测, 避免频繁重建
_SENDER_INDEX_CACHE: dict[int, str] | None = None
_SENDER_INDEX_FP: tuple = ()


def _build_sender_index(cache_dir: str) -> dict[int, str]:
    """从已解密的 contact.db 构建 {int_id -> 显示名} 字典。

    群聊消息中 real_sender_id 是整型 contact.id, 通过此索引可将 id 转为昵称/备注.
    id=1 固定映射为"我"(自己).

    Args:
        cache_dir: 已解密数据库的根缓存目录, 含 contact/contact.db。

    Returns:
        {id: 显示名} 字典, contact 优先于 stranger (同 id 不覆盖已有条目).
    """
    idx: dict[int, str] = {1: "我"}
    contact_db = os.path.join(cache_dir, "contact", "contact.db")
    if not os.path.exists(contact_db):
        return idx
    try:
        conn = sqlite3.connect(contact_db)
    except sqlite3.DatabaseError:
        return idx
    try:
        for tbl in ("contact", "stranger"):
            try:
                cols = {r[1] for r in conn.execute(f"PRAGMA table_info([{tbl}])").fetchall()}
            except sqlite3.OperationalError:
                continue
            if "id" not in cols or "username" not in cols:
                continue
            select = ["id", "username"]
            # 按需追加可选字段 (不同版本微信字段集不同)
            for opt in ("nick_name", "remark"):
                if opt in cols:
                    select.append(opt)
            sql = f"SELECT {', '.join(select)} FROM [{tbl}]"
            try:
                cur = conn.execute(sql)
            except sqlite3.OperationalError:
                continue
            for row in cur:
                rid = row[0]
                if not isinstance(rid, int):
                    try:
                        rid = int(rid)
                    except (TypeError, ValueError):
                        continue
                # contact 表优先于 stranger, 已有 id 且不是占位的不覆盖
                if rid in idx and idx[rid] != "我":
                    continue
                username = row[1] or ""
                name = ""
                # 显示名优先级: remark > nick_name
                for i, c in enumerate(select):
                    if c in ("remark", "nick_name") and row[i]:
                        name = str(row[i])
                        break
                idx[rid] = name or username or f"id={rid}"
    finally:
        conn.close()
    return idx


def get_sender_index(cache_dir: str) -> dict[int, str]:
    """带模块级缓存的 sender_id -> 显示名索引。

    用 contact.db 的 mtime+size 指纹做失效检测, 文件未变则直接返回缓存.
    """
    global _SENDER_INDEX_CACHE, _SENDER_INDEX_FP
    fp = _index_fingerprint(cache_dir)
    if _SENDER_INDEX_CACHE is not None and fp == _SENDER_INDEX_FP:
        return _SENDER_INDEX_CACHE
    _SENDER_INDEX_CACHE = _build_sender_index(cache_dir)
    _SENDER_INDEX_FP = fp
    return _SENDER_INDEX_CACHE


# ─── 1. 整库解密缓存 ───────────────────────────────────────
def _src_fingerprint(src: str, wal_src: str) -> str:
    """计算源 db 及其 WAL 文件的 mtime+size 指纹字符串。

    用于增量解密判断: 指纹不变则跳过重解密. 文件不存在时用 "-" 占位.

    Args:
        src: 源数据库绝对路径。
        wal_src: 对应 WAL 文件路径 (通常为 src + "-wal")。

    Returns:
        形如 "mtime_ns:size|mtime_ns:size" 的指纹字符串。
    """
    parts = []
    for p in (src, wal_src):
        try:
            st = os.stat(p)
            parts.append(f"{st.st_mtime_ns}:{st.st_size}")
        except OSError:
            parts.append("-")
    return "|".join(parts)


def decrypt_all_dbs(db_dir: str, key_map: dict[str, str], cache_dir: str | None = None, *, on_log=None) -> dict[str, str]:
    """批量解密 db_dir 下所有可识别的 .db 文件到 cache_dir。

    遍历 db_dir 的完整子目录树, 对每个 .db 文件:
      1. 读取文件头 16 字节作为 salt, 在 key_map 中查找对应解密 key;
      2. 调用 full_decrypt 进行页级 AES 解密, 输出到 cache_dir 中的同名路径;
      3. 若存在 -wal 文件, 调用 apply_wal 将 WAL 日志合并进解密副本;
      4. 将 mtime+size 指纹写入 .meta 文件, 下次调用时若指纹未变则跳过.

    增量策略: 微信历史分库不再写入时指纹不变可跳过, 仅活跃分库 (有新消息)
    会重解密. 可将整体耗时从全量的几十秒降到秒级.

    Args:
        db_dir: 微信原始数据库根目录 (含加密的 .db 文件)。
        key_map: {salt_hex: enc_key_hex} 映射, 由调用方从内存提取并传入。
        cache_dir: 解密输出目录; 为 None 时使用系统临时目录下的 gh_wx_cache。
        on_log: 可选日志接收函数，默认沿用 wlog；不修改模块全局日志函数。

    Returns:
        {相对路径: 解密后的绝对路径} 映射, 仅包含成功解密的文件。

    副作用:
        在 cache_dir 写入解密副本及 .meta 指纹文件; 通过 wlog 记录进度日志。
    """
    log = on_log or wlog
    out_dir = Path(cache_dir or os.path.join(tempfile.gettempdir(), "gh_wx_cache"))
    out_dir.mkdir(parents=True, exist_ok=True)
    mapping: dict[str, str] = {}
    skipped = 0
    decrypted = 0
    no_key = 0
    no_key_names: list[str] = []
    for root, _dirs, names in os.walk(db_dir):
        for name in names:
            # 跳过 WAL/SHM 辅助文件, 只处理主 .db 文件
            if not name.endswith(".db") or name.endswith(("-wal", "-shm")):
                continue
            src = os.path.join(root, name)
            try:
                # 文件头 16 字节即 SQLCipher 的 salt
                with open(src, "rb") as f:
                    salt = f.read(16).hex()
            except OSError:
                continue
            key_hex = key_map.get(salt)
            if not key_hex:
                # key_map 中无对应 salt, 说明此库不属于当前微信账号或 key 未提取到
                no_key += 1
                if len(no_key_names) < 8:
                    no_key_names.append(os.path.relpath(src, db_dir))
                continue
            try:
                key = bytes.fromhex(key_hex)
            except ValueError:
                continue
            rel = os.path.relpath(src, db_dir)
            dst = out_dir / rel
            dst.parent.mkdir(parents=True, exist_ok=True)

            wal_src = src + "-wal"
            # .meta 文件存储上次解密时的 mtime+size 指纹
            meta = Path(str(dst) + ".meta")
            fp = _src_fingerprint(src, wal_src)
            if dst.exists() and meta.exists():
                try:
                    if meta.read_text() == fp:
                        # 指纹未变, 缓存仍然有效, 直接复用
                        mapping[rel] = str(dst)
                        skipped += 1
                        continue
                except OSError:
                    pass

            try:
                full_decrypt(src, str(dst), key)
            except Exception as e:
                # 跳过损坏 / 不可解密的, 不中断整体流程
                log("warning", f"[wechat_native] decrypt failed {rel}: {e}")
                continue
            if os.path.exists(wal_src):
                try:
                    # WAL 中可能有未提交到主库的最新消息, 必须合并
                    apply_wal(wal_src, str(dst), key)
                except Exception as e:
                    log("warning", f"[wechat_native] wal apply failed {rel}: {e}")
            try:
                meta.write_text(fp)
            except OSError:
                pass
            decrypted += 1
            mapping[rel] = str(dst)
    if skipped or decrypted:
        log("info", f"[wechat_native] 解密: 跳过未变化 {skipped} 个, 重解密 {decrypted} 个")
    if no_key:
        log(
            "warning",
            f"[wechat_native] {no_key} 个库无对应密钥被跳过 ({', '.join(no_key_names)}); "
            "若缺 contact/session 导致昵称或通讯录不可用, 请在「密码」页重新获取密钥 (多次抓取可累积覆盖)",
        )
    return mapping


# ─── 2. 工具 ─────────────────────────────────────────────
def _msg_table_name(chat_name: str) -> str:
    """计算指定 username 对应的消息表名。

    微信 4.x 表名格式: Msg_<md5(username)>, username 通常是 wxid_xxx 或群 chatroom@chatroom.

    Args:
        chat_name: 聊天对象的微信 username (wxid / chatroom ID)。

    Returns:
        形如 "Msg_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx" 的表名字符串。
    """
    table_hash = hashlib.md5(chat_name.encode("utf-8")).hexdigest()
    return f"Msg_{table_hash}"


# ─── username 反查 (md5(username) -> 显示名) ───────────────
# 模块级缓存: cache_dir 指纹(mtime+size) 变了就重建.
_USERNAME_INDEX_CACHE: dict[str, dict[str, str]] | None = None
_USERNAME_INDEX_FP: tuple = ()


def _index_fingerprint(cache_dir: str) -> tuple:
    """计算联系人索引相关数据库文件的 mtime+size 指纹元组, 用于缓存失效检测。"""
    candidates = [
        os.path.join(cache_dir, "contact", "contact.db"),
        os.path.join(cache_dir, "session", "session.db"),
    ]
    fp = []
    for p in candidates:
        try:
            st = os.stat(p)
            fp.append((p, st.st_mtime_ns, st.st_size))
        except OSError:
            fp.append((p, 0, 0))
    return tuple(fp)


def _build_username_index(cache_dir: str) -> dict[str, dict[str, str]]:
    """从 contact.db 构建 {md5(username): {username, name}} 正向索引。

    消息表名 Msg_<hash> 中的 hash 即 md5(username), 通过此索引可将表名还原为
    联系人 username 和显示名 (备注 > 昵称 > alias > username).

    Args:
        cache_dir: 已解密数据库根目录, 含 contact/contact.db。

    Returns:
        {md5_hex: {"username": ..., "name": ...}} 字典.
        contact 表优先于 stranger (同 hash 不覆盖).
    """
    idx: dict[str, dict[str, str]] = {}
    contact_db = os.path.join(cache_dir, "contact", "contact.db")
    if not os.path.exists(contact_db):
        return idx
    try:
        conn = sqlite3.connect(contact_db)
    except sqlite3.DatabaseError:
        return idx
    try:
        for tbl in ("contact", "stranger"):
            try:
                cols = {r[1] for r in conn.execute(f"PRAGMA table_info([{tbl}])").fetchall()}
            except sqlite3.OperationalError:
                continue
            if "username" not in cols:
                continue
            select_cols = ["username"]
            # 按需追加可选字段, 避免在旧版微信数据库中报列不存在
            for opt in ("nick_name", "remark", "alias"):
                if opt in cols:
                    select_cols.append(opt)
            sql = f"SELECT {', '.join(select_cols)} FROM [{tbl}]"
            try:
                cur = conn.execute(sql)
            except sqlite3.OperationalError:
                continue
            col_idx = {c: i for i, c in enumerate(select_cols)}
            for row in cur:
                u = row[col_idx["username"]]
                if not u or not isinstance(u, str):
                    continue
                # 显示名优先级: remark > nick_name > alias > username
                name = ""
                for k in ("remark", "nick_name", "alias"):
                    if k in col_idx:
                        v = row[col_idx[k]]
                        if v:
                            name = str(v)
                            break
                if not name:
                    name = u
                h = hashlib.md5(u.encode("utf-8")).hexdigest()
                # contact 表优先于 stranger, 同 hash 已存在不覆盖
                if h not in idx:
                    idx[h] = {"username": u, "name": name}
    finally:
        conn.close()
    return idx


def get_username_index(cache_dir: str) -> dict[str, dict[str, str]]:
    """带模块级缓存的 md5(username) -> {username, name} 索引。

    用 contact.db 和 session.db 的 mtime+size 指纹做失效检测.
    """
    global _USERNAME_INDEX_CACHE, _USERNAME_INDEX_FP
    fp = _index_fingerprint(cache_dir)
    if _USERNAME_INDEX_CACHE is not None and fp == _USERNAME_INDEX_FP:
        return _USERNAME_INDEX_CACHE
    _USERNAME_INDEX_CACHE = _build_username_index(cache_dir)
    _USERNAME_INDEX_FP = fp
    return _USERNAME_INDEX_CACHE


# ─── 会话名快照 (退群/被踢后微信会删掉 contact/session 记录, 名字随之丢失) ───
def _session_names_path(cache_dir: str) -> Path:
    """会话名快照文件, 放在 decrypted 缓存的同级 (config 目录下), 清缓存不丢。"""
    return Path(cache_dir).resolve().parent / "session_names.json"


def _load_session_names(cache_dir: str) -> dict[str, dict[str, str]]:
    """读 {chat_hash: {username, name}} 快照; 文件缺失/损坏时返回空表。"""
    try:
        data = json.loads(_session_names_path(cache_dir).read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): v for k, v in data.items() if isinstance(v, dict)}


def _save_session_names(cache_dir: str, snap: dict[str, dict[str, str]]) -> None:
    """原子写快照; 失败静默 (快照只是增强, 不影响主流程)。"""
    path = _session_names_path(cache_dir)
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(json.dumps(snap, ensure_ascii=False), "utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def resolve_chat_name(cache_dir: str, raw_input: str) -> str:
    """将用户输入的聊天标识解析为真实 username (即 md5 的输入源)。

    支持三种输入形式 (按优先级):
      1. 32 位小写 hex -- 视为已是 md5 hash, 直接反查 username;
      2. 直接 wxid / chatroom ID -- md5 后在索引中命中则原样返回;
      3. 昵称 / 备注关键词 -- 遍历索引, 返回首个包含该字符串的 username.

    Args:
        cache_dir: 已解密数据库根目录。
        raw_input: 用户输入的聊天标识。

    Returns:
        真实 username 字符串; 无法解析时原样返回 raw_input.
    """
    s = (raw_input or "").strip()
    if not s:
        return ""
    idx = get_username_index(cache_dir)
    # hex hash 反查
    if len(s) == 32 and all(c in "0123456789abcdef" for c in s.lower()):
        ent = idx.get(s.lower())
        return ent["username"] if ent else s
    # 直接当 username (md5 算出的 hash 已存在)
    if hashlib.md5(s.encode("utf-8")).hexdigest() in idx:
        return s
    # 模糊匹配 name (备注 / 昵称)
    for ent in idx.values():
        if s in ent["name"] or ent["name"] == s:
            return ent["username"]
    return s   # 找不到, 原样返回 (后续 md5 算出的 hash 没表会跳过)


def _format_ts(ts: int) -> str:
    """将 Unix 时间戳格式化为本地时间字符串 "YYYY-MM-DD HH:MM:SS"。"""
    if not ts:
        return ""
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, OSError):
        return ""


def _date_to_unix(date_str: str, end: bool = False) -> int:
    """将日期/时间字符串解析为 Unix 时间戳 (本地时区)。

    支持格式 (按优先级):
      * "2025-11-28T14:30"      datetime-local 输入
      * "2025-11-28 14:30:00"   带秒
      * "2025-11-28 14:30"      带时分
      * "2025-11-28"            仅日期 (end=True 自动取 23:59:59)

    end=True 且只给了日期: 取当天结束.
    """
    if not date_str:
        return 0
    s = date_str.strip()
    has_time = "T" in s or (" " in s and ":" in s)
    fmts = (
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
    )
    dt = None
    for f in fmts:
        try:
            dt = datetime.strptime(s, f)
            break
        except ValueError:
            continue
    if dt is None:
        return 0
    if end and not has_time:
        dt = dt.replace(hour=23, minute=59, second=59)
    return int(dt.timestamp())


def _columns_of(conn: sqlite3.Connection, table: str) -> list[str]:
    """返回指定表的所有列名列表 (通过 PRAGMA table_info 查询)。"""
    return [r[1] for r in conn.execute(f"PRAGMA table_info([{table}])").fetchall()]


def _safe_text(val) -> str:
    """安全地将任意数据库字段值转为字符串, 处理 None/bytes/其他类型。"""
    if val is None:
        return ""
    if isinstance(val, bytes):
        try:
            return val.decode("utf-8", errors="replace")
        except Exception:
            return val.hex()
    return str(val)


# ─── 3. Sessions 列表 ─────────────────────────────────────
def list_sessions(cache_dir: str) -> list[dict]:
    """列举缓存目录下所有会话及统计信息。

    遍历所有 message_*.db, 聚合每个 Msg_* 表的消息数和最新时间戳.
    表名是 md5(username) 不含明文, 故再 join get_username_index (键正是
    md5(username)) 把 hash 反查成真实 username 和显示名; 通讯录未命中的会话
    再用 session_names.json 名字快照兜底 (退群后微信删除 contact 记录,
    快照保留最后已知名字), 仍未命中 display_name 才回退为 hash 本身.
    chat_name 字段仍保留 hash 以向后兼容.

    Args:
        cache_dir: 已解密数据库根目录。

    Returns:
        按最后消息时间倒序排列的会话列表, 每项含:
          chat_name (32 位 hash 占位) / username (真实 wxid, 空=未命中) /
          display_name (显示名, 无匹配回退 hash) / message_count / last_message_time。
    """
    sessions: dict[str, dict] = {}
    for root, _dirs, names in os.walk(cache_dir):
        for name in names:
            if not _MSG_DB_RE.search(name):
                continue
            path = os.path.join(root, name)
            try:
                with closing(sqlite3.connect(path)) as conn:
                    rows = conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'"
                    ).fetchall()
                    for (tname,) in rows:
                        if not _SAFE_TABLE_RE.fullmatch(tname):
                            continue
                        cols = _columns_of(conn, tname)
                        if "create_time" in cols:
                            r = conn.execute(
                                f"SELECT COUNT(*), MAX(create_time) FROM [{tname}]"
                            ).fetchone()
                        else:
                            r = (conn.execute(f"SELECT COUNT(*) FROM [{tname}]").fetchone()[0], 0)
                        cnt, max_ct = (r[0] or 0), (r[1] or 0)
                        cur = sessions.get(tname)
                        if cur is None or (max_ct and max_ct > cur["_max_ct"]):
                            sessions[tname] = {
                                "chat_hash": tname[4:],   # 去掉 Msg_ 前缀
                                "message_count": cnt,
                                "last_message_time": _format_ts(max_ct),
                                "_max_ct": max_ct,
                            }
            except sqlite3.DatabaseError:
                continue
    idx = get_username_index(cache_dir)
    out = []
    for s in sessions.values():
        s.pop("_max_ct", None)
        h = s["chat_hash"]
        ent = idx.get(h)
        out.append({
            "chat_name": h,                              # 保留: 32 位 hash, 向后兼容旧前端
            "username": ent["username"] if ent else "",  # 真实 wxid/chatroom; 空=通讯录未命中
            "display_name": ent["name"] if ent else h,   # 显示名; 无匹配回退 hash
            "message_count": s["message_count"],
            "last_message_time": s["last_message_time"],
        })
    # 名字快照: 已解析的会话沉淀下来; 通讯录未命中的 (退群/被踢后微信删了
    # contact/session 记录, 本地库里已查不到名字) 用快照里最后已知的名字兜底,
    # 避免直接裸显示 32 位 hash
    snap = _load_session_names(cache_dir)
    dirty = False
    for r in out:
        h = r["chat_name"]
        if r["username"]:
            ent = snap.get(h)
            if ent is None or ent.get("username") != r["username"] or ent.get("name") != r["display_name"]:
                snap[h] = {"username": r["username"], "name": r["display_name"]}
                dirty = True
        else:
            ent = snap.get(h)
            if ent:
                r["username"] = ent.get("username", "")
                r["display_name"] = ent.get("name") or h
    if dirty:
        _save_session_names(cache_dir, snap)
    out.sort(key=lambda r: r["last_message_time"], reverse=True)
    return out


# ─── 4. 搜索消息 ─────────────────────────────────────────
def search_messages(
    cache_dir: str,
    *,
    start_date: str = "",
    end_date: str = "",
    chat_name: str = "",
    sender_name: str = "",
    exclude_contacts: str = "",
    keyword: str = "",
    message_type: str | int | None = None,
    exclude_self: bool = False,
    dedup_day: bool = False,
    dedup_content_latest: bool = False,
    dedup_content_earliest: bool = False,
    limit: int = 5000,
    full_content: bool = False,
    sender_from_raw_prefix: bool = False,
) -> list[dict]:
    """在已解密的 message_*.db 中多条件检索聊天记录。

    分库并行扫描 cache_dir 下所有 message_*.db 的 Msg_* 表, 对每条消息:
      - 在 SQL 层按 create_time 范围 / exclude_self 过滤, 按 sort_seq 索引倒序
        (与 create_time 倒序语义一致, 避免每表 TEMP B-TREE 全排序);
      - 在 Python 层按消息类型 / keyword / 发送人 / 聊天对象做进一步过滤
        (类型与 keyword 提前, 非命中行不付 md5/联系人反查开销);
      - 解压 zstd BLOB 并格式化内容摘要;
      - 通过 username 索引将 hash 还原为显示名.

    单 chat_name 时定位到唯一表跳过无关表, 多 chat_name 时全表扫再按名过滤.

    Args:
        cache_dir: 已解密数据库根目录。
        start_date: 查询起始时间, 格式见 _date_to_unix (留空不限)。
        end_date: 查询截止时间 (包含当天末尾)。
        chat_name: 聊天对象标识, 多个逗号分隔; 支持 wxid/昵称/备注/hash (留空=不限)。
        sender_name: 发送人过滤, 多个逗号分隔, 子串匹配; 留空不限。
        exclude_contacts: 排除的聊天对象, 多个逗号分隔。
        keyword: 消息内容关键词, 子串匹配 (在格式化后的摘要中搜索)。
        message_type: 消息类型 ID 过滤; None/""/0 = 不限。
        exclude_self: 为 True 时过滤掉自己 (real_sender_id=1) 发送的消息。
        dedup_day: 同会话同天同内容去重 (保留一条)。
        dedup_content_latest: 跨会话按内容去重, 保留时间戳最大的一条。
        dedup_content_earliest: 跨会话按内容去重, 保留时间戳最小的一条 (与 latest 互斥, latest 优先)。
        limit: 返回结果上限 (含去重前), 默认 5000; **<=0 表示不限量返回全部命中**。
        full_content: 保留格式化正文，不做返回值的 500 字摘要截断；默认维持上游行为。
        sender_from_raw_prefix: 群卡片从原始正文前缀还原发送人，避免 XML 摘要丢失前缀后误用 contact.id；默认关闭。

    Returns:
        按 create_time 全局倒序 (最新在前, 跨分库统一排序) 的消息字典列表, 每项含:
          time / create_ts / sender / type / type_id / content / chat_name.
    """
    ts_lo = _date_to_unix(start_date, end=False)
    ts_hi = _date_to_unix(end_date, end=True)

    # 加载联系人和发送人索引 (各自带模块级缓存, 不重复 I/O)
    name_idx = get_username_index(cache_dir)
    sender_idx = get_sender_index(cache_dir)

    # 将 message_type 参数解析为整型过滤值; 空/0/"0" 表示不过滤
    type_filter: int | None = None
    if message_type not in (None, "", 0, "0"):
        try:
            type_filter = int(message_type)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            type_filter = None

    # 解析逗号分隔的多值参数
    chat_names_raw = [c.strip() for c in (chat_name or "").split(",") if c.strip()]
    sender_names = {s.strip() for s in (sender_name or "").split(",") if s.strip()}
    exclude_set = {x.strip() for x in (exclude_contacts or "").split(",") if x.strip()}

    # 单 chat: 定位到唯一的 Msg_* 表, 跳过无关表; 多 chat: 全表扫后按名过滤
    if len(chat_names_raw) == 1:
        resolved_chat = resolve_chat_name(cache_dir, chat_names_raw[0])
        target_table = _msg_table_name(resolved_chat) if resolved_chat else None
        chat_filter_set: set[str] = set()
    elif len(chat_names_raw) > 1:
        target_table = None
        resolved_chat = ""
        # 多个 chat: 后续按 chat_display 过滤, 同时收集 username 和原始输入两种形式
        chat_filter_set = set()
        for c in chat_names_raw:
            r = resolve_chat_name(cache_dir, c)
            if r:
                chat_filter_set.add(r)
            chat_filter_set.add(c)
    else:
        # 未指定 chat: 全表扫, 无 chat 过滤
        target_table = None
        resolved_chat = ""
        chat_filter_set = set()

    # limit<=0 = 不限量; cap 有值时用于 SQL LIMIT 与早停
    cap = limit if limit and limit > 0 else None

    # 先收集全部 message_*.db 路径, 供分库并行扫描
    db_paths: list[str] = []
    for root, _dirs, names in os.walk(cache_dir):
        for name in names:
            if _MSG_DB_RE.search(name):
                db_paths.append(os.path.join(root, name))

    def _scan_db(path: str, room: int | None) -> list[dict]:
        """扫描单个 message db, 返回本库命中的消息列表。

        room: 本库还需要的条数 (跨库全局早停的剩余额度); None = 不限量。

        性能取向 (Windows 大库实测痛点):
          - ORDER BY sort_seq DESC 吃微信自带的 SORTSEQ 索引 (排序语义与
            create_time 倒序一致), 避免每表 TEMP B-TREE 全排序;
          - 只 SELECT 用到的列, 不搬运 packed_info_data 等无关 BLOB;
          - exclude_self 下推 SQL (real_sender_id != 1), Python 层保留兜底;
          - 行处理先过 type/keyword 再做 md5/索引反查, 非命中行不付反查开销。
        """
        found: list[dict] = []
        try:
            conn = sqlite3.connect(path)
        except sqlite3.DatabaseError:
            return found
        try:
            if target_table:
                if not _SAFE_TABLE_RE.fullmatch(target_table):
                    return found
                exists = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                    (target_table,),
                ).fetchone()
                if not exists:
                    return found
                tables = [target_table]
            else:
                tables = [
                    t for (t,) in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'"
                    ).fetchall() if _SAFE_TABLE_RE.fullmatch(t)
                ]

            for t in tables:
                if room is not None and len(found) >= room:
                    break
                cols = set(_columns_of(conn, t))
                have_ct = "create_time" in cols
                # 列裁剪: 只取行处理用到的列 (按存在性动态适配新旧库列名)
                want = [c for c in ("local_type", "Type", "real_sender_id", "create_time",
                                    "message_content", "compress_content", "source") if c in cols]
                if not want:
                    continue
                pos = {c: i for i, c in enumerate(want)}
                where = []
                params: list = []
                # 时间范围过滤在 SQL 层完成; keyword 必须在 Python 解压后才能过滤
                if have_ct and ts_lo:
                    where.append("create_time >= ?")
                    params.append(ts_lo)
                if have_ct and ts_hi:
                    where.append("create_time <= ?")
                    params.append(ts_hi)
                if exclude_self and "real_sender_id" in cols:
                    where.append("real_sender_id != 1")
                sql = f"SELECT {', '.join(f'[{c}]' for c in want)} FROM [{t}]"
                if where:
                    sql += " WHERE " + " AND ".join(where)
                # sort_seq 与 create_time 单调对应 (微信用它排消息流) 且自带索引,
                # 优先用它排序避免 TEMP B-TREE; 老库无该列时回退 create_time
                if "sort_seq" in cols:
                    sql += " ORDER BY sort_seq DESC"
                elif have_ct:
                    sql += " ORDER BY create_time DESC"
                if room is not None:
                    # 有 keyword 时多取 5 倍候选行, 补偿 keyword 过滤后命中率不足 100%
                    sql += f" LIMIT {max(1, (room - len(found)) * (5 if keyword else 1))}"
                try:
                    cur = conn.execute(sql, params)
                except sqlite3.OperationalError:
                    continue
                thash = t[4:]
                chat_ent = name_idx.get(thash)
                is_group = bool(chat_ent and "@chatroom" in chat_ent["username"])
                for row in cur:
                    type_id = row[pos["local_type"]] if "local_type" in pos else None
                    if not type_id and "Type" in pos:
                        type_id = row[pos["Type"]]
                    try:
                        type_id_int = int(type_id) if type_id else 0
                    except (TypeError, ValueError):
                        type_id_int = 0
                    type_id_int = _normalize_type(type_id_int)
                    # 类型过滤最便宜, 放在解压/格式化之前
                    if type_filter is not None and type_id_int != type_filter:
                        continue
                    # 按消息类型格式化内容 (zstd 解压 + XML 摘要)
                    raw = None
                    for c in ("message_content", "compress_content", "source"):
                        if c in pos and row[pos[c]]:
                            raw = row[pos[c]]
                            break
                    content = format_content(type_id_int, raw or "")
                    # 群聊文本消息内容头部带 "wxid:\n" 前缀, keyword 只应匹配剥离后的正文
                    group_wxid = ""
                    if is_group and isinstance(content, str):
                        head, sep, body = content.partition(":\n")
                        if sep and 0 < len(head) < 80 and "\n" not in head and " " not in head:
                            group_wxid = head.strip()
                            content = body
                    if is_group and sender_from_raw_prefix and not group_wxid:
                        # real_sender_id 属于消息库自身的编号，不能保证与 contact.id 相同。
                        # 仅使用原消息已有前缀做显示名映射，不改变查询或正文格式化。
                        raw_text = _decompress_content(raw or "")
                        head, sep, _ = raw_text.partition(":\n")
                        if sep and re.fullmatch(r"[A-Za-z0-9_@.\-]{1,79}", head):
                            group_wxid = head
                    # keyword 过滤提前: 非命中行不再付出 md5/索引反查的开销
                    if keyword and keyword not in content:
                        continue
                    ct = (row[pos["create_time"]] if "create_time" in pos else 0) or 0
                    sender_id = row[pos["real_sender_id"]] if "real_sender_id" in pos else None
                    try:
                        sid_int = int(sender_id) if sender_id is not None else 0
                    except (TypeError, ValueError):
                        sid_int = 0
                    if group_wxid:
                        h = hashlib.md5(group_wxid.encode("utf-8")).hexdigest()
                        ent_s = name_idx.get(h)
                        sender = (ent_s["name"] if ent_s else group_wxid)
                    elif sid_int == 1:
                        sender = "我"
                    elif is_group:
                        # 非文本消息或者解析不出前缀: 兜底 contact.id 反查
                        sender = (f"id={sid_int}（发送人未核实）" if sender_from_raw_prefix
                                  else sender_idx.get(sid_int) or f"id={sid_int}")
                    else:
                        # 私聊: 非自己即对方
                        sender = chat_ent["name"] if chat_ent else f"id={sid_int}"
                    # 排除自己 (SQL 已下推 real_sender_id != 1, 这里兜底列缺失的旧库)
                    if exclude_self and (sid_int == 1 or sender == "我"):
                        continue
                    # 发送人过滤 (多个 OR)
                    if sender_names and not any(sn in sender for sn in sender_names):
                        continue
                    # 反查 chat 显示名: 表名后 32 位 = md5(username)
                    if chat_ent:
                        chat_display = chat_ent["name"] if chat_ent["name"] != chat_ent["username"] else chat_ent["username"]
                    elif resolved_chat:
                        chat_display = resolved_chat
                    else:
                        chat_display = thash
                    # 多 chat 过滤 (语义同旧实现: 有联系人条目时额外允许 username 精确匹配)
                    if chat_filter_set:
                        if chat_ent:
                            hit = any(c in chat_display or c == chat_ent.get("username", "") for c in chat_filter_set)
                        else:
                            hit = any(c == chat_display for c in chat_filter_set)
                        if not hit:
                            continue
                    # 排除联系人
                    if exclude_set and any(x in chat_display for x in exclude_set):
                        continue
                    found.append({
                        "time": _format_ts(ct),
                        "create_ts": ct,
                        "sender": sender,
                        "type": MSG_TYPE_NAMES.get(type_id_int, f"type={type_id_int}"),
                        "type_id": type_id_int,
                        "content": content if full_content else content[:500],
                        "chat_name": chat_display,
                    })
                    if room is not None and len(found) >= room:
                        break
        finally:
            conn.close()
        return found

    # 顺序扫描 + 跨库全局早停: 实测线程池并行在 GIL 下对行处理 (纯 Python 正则/
    # 解码) 无增益, 反而破坏跨库早停语义 (每库各攒满 limit, 有 cap 场景慢 2-3 倍),
    # 故保持顺序; cap 场景攒满立即停止扫剩余分库。
    out: list[dict] = []
    for p in db_paths:
        if cap is not None and len(out) >= cap:
            break
        out.extend(_scan_db(p, None if cap is None else cap - len(out)))

    # 全局时间倒序: 旧实现按目录遍历序拼接、分库之间乱序; 并行后统一排序,
    # 输出保证全局 create_time 倒序 (最新在前), 有 cap 时对合并超集截断
    out.sort(key=lambda m: m.get("create_ts", 0), reverse=True)
    if cap is not None:
        out = out[:cap]

    # ─── 去重后处理 ───
    if dedup_day:
        # 同会话同天同内容 key 去重, 保留首次出现
        seen: set[str] = set()
        unique: list[dict] = []
        for m in out:
            day = (m.get("time") or "")[:10]
            key = f"{m.get('chat_name', '')}|{day}|{m.get('content', '')}"
            if key in seen:
                continue
            seen.add(key)
            unique.append(m)
        out = unique
    if dedup_content_latest or dedup_content_earliest:
        # 先按时间排序: latest 模式保留时间戳最大的 (reverse=True 先出现的即最大)
        reverse = bool(dedup_content_latest)
        out.sort(key=lambda m: m.get("create_ts", 0), reverse=reverse)
        seen_c: set[str] = set()
        unique = []
        for m in out:
            c = (m.get("content") or "").strip()
            if not c or c in seen_c:
                continue
            seen_c.add(c)
            unique.append(m)
        # 还原默认时间倒序 (最新消息在前)
        out = sorted(unique, key=lambda m: m.get("create_ts", 0), reverse=True)

    return out if cap is None else out[:cap]
