"""对沿用的 gh_ui 链路做加密库/WAL/长接龙集成测试；不冒充真实账号测试。"""
from __future__ import annotations

import ctypes
import ctypes.util
import hashlib
import hmac
import json
from pathlib import Path
import sqlite3
import struct
import shlex
import sys
import tempfile
import unittest
from unittest.mock import patch

from Crypto.Cipher import AES
import zstandard

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from group_msg_monitor.relay_core import atomic_json, parse_bjt, update_file
from group_msg_monitor.wechat_source import WeChatSource
from test_workflow import make_roster, make_target


def sql_quote(value):
    return "'" + str(value).replace("'", "''") + "'"


def _sqlite3_library():
    """Locate a native sqlite3 shared library for SQLCipher-page fixtures.

    Windows Python usually ships only `_sqlite3.pyd`, so find_library may return
    None; probe common DLL locations before skipping the encrypted-db tests.
    """
    name = ctypes.util.find_library('sqlite3')
    candidates = [Path(name)] if name else []
    roots = [Path(sys.base_prefix), Path(sys.prefix), Path(sys.executable).resolve().parent]
    for root in roots:
        candidates.extend([
            root / 'sqlite3.dll',
            root / 'DLLs' / 'sqlite3.dll',
            root / 'Library' / 'bin' / 'sqlite3.dll',
        ])
    for path in candidates:
        if path and path.is_file():
            return ctypes.CDLL(str(path))
    raise unittest.SkipTest('native sqlite3 shared library not found; encrypted DB fixtures need it on Windows')


def plaintext_database(path, statements):
    """生成和 SQLCipher 相同的 4096 页/80 保留字节 SQLite 测试库。"""
    lib = _sqlite3_library()
    lib.sqlite3_open.argtypes = [ctypes.c_char_p, ctypes.POINTER(ctypes.c_void_p)]
    lib.sqlite3_file_control.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int, ctypes.c_void_p]
    lib.sqlite3_exec.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_char_p)]
    lib.sqlite3_close.argtypes = [ctypes.c_void_p]
    db = ctypes.c_void_p()
    if lib.sqlite3_open(str(path).encode(), ctypes.byref(db)):
        raise RuntimeError('fixture sqlite open failed')
    try:
        reserve = ctypes.c_int(80)
        if lib.sqlite3_file_control(db, b'main', 38, ctypes.byref(reserve)):
            raise RuntimeError('fixture sqlite reserve bytes failed')
        err = ctypes.c_char_p()
        sql = 'PRAGMA page_size=4096;' + ';'.join(statements) + ';'
        if lib.sqlite3_exec(db, sql.encode(), None, None, ctypes.byref(err)):
            raise RuntimeError(err.value.decode())
    finally:
        lib.sqlite3_close(db)
    raw = path.read_bytes()
    assert raw[20] == 80
    return raw


def encrypted_page(plain, number, key, salt):
    iv = hashlib.sha256(salt + struct.pack('<I', number)).digest()[:16]
    start = 16 if number == 1 else 0
    cipher = AES.new(key, AES.MODE_CBC, iv).encrypt(plain[start:4016])
    mac_salt = bytes(b ^ 0x3a for b in salt)
    mac_key = hashlib.pbkdf2_hmac('sha512', key, mac_salt, 2, dklen=32)
    mac = hmac.new(mac_key, cipher + iv + struct.pack('<I', number), hashlib.sha512).digest()
    return (salt if number == 1 else b'') + cipher + iv + mac


def encrypt_database(plain, path, key, salt):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b''.join(encrypted_page(plain[i:i+4096], i//4096+1, key, salt) for i in range(0, len(plain), 4096)))


def encrypted_wal(plain, path, key, salt):
    # 与上游消费的 WAL 帧格式一致；每页均为测试生成的已提交快照。
    header = bytearray(32)
    struct.pack_into('>IIIIII', header, 0, 0x377f0682, 3007000, 4096, 0, 1234, 5678)
    output = bytes(header)
    pages = len(plain) // 4096
    for i in range(pages):
        frame = struct.pack('>IIIIII', i+1, pages if i == pages-1 else 0, 1234, 5678, 0, 0)
        output += frame + encrypted_page(plain[i*4096:(i+1)*4096], i+1, key, salt)
    path.write_bytes(output)


class WeChatSourceTests(unittest.TestCase):
    def setUp(self):
        # Windows may keep decrypted SQLite handles briefly; ignore cleanup races.
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db_dir = self.root / 'db_storage'
        self.local = self.root / 'local'
        self.key = hashlib.sha256(b'test-only-encryption-key').digest()
        self.keys = {}
        self.username = 'test_gh_relay@chatroom'
        self.table = 'Msg_' + hashlib.md5(self.username.encode()).hexdigest()
        self.timestamp = int(parse_bjt('2026-10-26 08:20:00').timestamp())
        self.long_text = '#接龙\n【富国基金】\n1. 赵阳@国海策略\n2. 张建民@国海通信 已要'
        contact = plaintext_database(self.root/'plain_contact.db', [
            'CREATE TABLE contact (id INTEGER PRIMARY KEY,username TEXT,nick_name TEXT,remark TEXT,alias TEXT)',
            f'INSERT INTO contact VALUES(2,{sql_quote(self.username)},\'GH派点反馈群\',\'\',\'\')'])
        self.add_db('contact/contact.db', contact)
        base = self.message_plain('base', [self.long_text])
        self.add_db('message/message_0.db', base)
        self.save_config()
        self.source = WeChatSource(self.local)

    def message_plain(self, name, contents, local_type=1):
        statements = [f'CREATE TABLE [{self.table}] (local_id INTEGER,local_type INTEGER,real_sender_id INTEGER,create_time INTEGER,sort_seq INTEGER,message_content BLOB)']
        for index, text in enumerate(contents):
            compressed = zstandard.ZstdCompressor().compress(('test_sender:\n'+text).encode()).hex()
            statements.append(f"INSERT INTO [{self.table}] VALUES({index+1},{local_type},2,{self.timestamp+index},{index+1},X'{compressed}')")
        return plaintext_database(self.root/f'{name}.db', statements)

    def add_db(self, relative, plain):
        salt = hashlib.md5(relative.encode()).digest()
        encrypt_database(plain, self.db_dir / relative, self.key, salt)
        self.keys[salt.hex()] = self.key.hex()

    def save_config(self):
        atomic_json(self.local/'wechat.json', {'db_dir': str(self.db_dir), 'keys': self.keys})

    def fetch(self):
        return self.source.fetch(parse_bjt('2026-10-26 00:00:00'), parse_bjt('2026-10-26 09:00:00'), 'GH派点反馈群')

    def test_encrypted_db_group_lookup_and_message_tail(self):
        result = self.fetch()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['content'], self.long_text)
        self.assertIn('张建民', result[0]['content'][-40:])

    def test_long_relay_keeps_tail_and_updates_person_after_500(self):
        text = '#接龙\n【富国基金】\n' + ('公告内容' * 190) + '\n1. 赵阳'
        self.add_db('message/message_0.db', self.message_plain('long', [text]))
        self.assertEqual(self.fetch()[0]['content'], text)
        roster, target = self.root/'roster.xlsx', self.root/'target.xlsx'
        make_roster(roster, ['赵阳', '张建民'])
        make_target(target, ['赵阳', '张建民'])
        result = update_file(target, roster, self.source.fetch, first_start='2026-10-26', end='2026-10-26 09:00:00')
        self.assertEqual(result['cleared_count'], 1)
        self.assertEqual(result['changes'][0]['person'], '赵阳')

    def test_default_upstream_summary_is_preserved(self):
        from group_msg_monitor.wechat_source import native
        text = '#接龙\n【富国基金】\n' + ('公告内容' * 190) + '\n1. 赵阳'
        self.add_db('message/message_0.db', self.message_plain('long_summary', [text]))
        cache = self.source.refresh()
        default = native.search_messages(str(cache), chat_name=self.username, limit=0)
        full = native.search_messages(str(cache), chat_name=self.username, limit=0, full_content=True)
        self.assertEqual(default[0]['content'], text[:500])
        self.assertEqual(full[0]['content'], text)

    def test_long_appmsg_relay_keeps_final_name(self):
        text = '#接龙\n【富国基金】\n' + ('公告内容' * 190) + '\n1. 赵阳'
        xml = '<msg><appmsg><title><![CDATA['+text+']]></title><type>53</type></appmsg></msg>'
        self.add_db('message/message_0.db', self.message_plain('long_appmsg', [xml], local_type=49))
        content = self.fetch()[0]['content']
        self.assertGreater(len(content), 500)
        self.assertTrue(content.endswith('1. 赵阳'))

    def test_group_card_sender_uses_raw_prefix_instead_of_unrelated_contact_id(self):
        contact = plaintext_database(self.root/'contact_sender.db', [
            'CREATE TABLE contact (id INTEGER PRIMARY KEY,username TEXT,nick_name TEXT,remark TEXT,alias TEXT)',
            f'INSERT INTO contact VALUES(2,{sql_quote(self.username)},\'GH派点反馈群\',\'\',\'\')',
            "INSERT INTO contact VALUES(14,'test_sender','真实发送者','','')"])
        self.add_db('contact/contact.db', contact)
        xml = '<msg><appmsg><title><![CDATA['+self.long_text+']]></title><type>53</type></appmsg></msg>'
        self.add_db('message/message_0.db', self.message_plain('sender_card', [xml], local_type=49))
        self.assertEqual(self.fetch()[0]['sender'], '真实发送者')
        cache = self.source.refresh()
        from group_msg_monitor.wechat_source import native
        default = native.search_messages(str(cache), chat_name=self.username, limit=0, full_content=True)
        self.assertEqual(default[0]['sender'], 'GH派点反馈群')  # 默认行为未改；适配层明确开启修复。

    def test_new_messages_in_encrypted_wal_are_read_and_refreshed(self):
        self.assertEqual(len(self.fetch()), 1)
        later = '#接龙\n【汇添富权益】\n1. 赵阳'
        updated = self.message_plain('updated', [self.long_text, later])
        relative = 'message/message_0.db'
        encrypted_wal(updated, Path(str(self.db_dir/relative)+'-wal'), self.key, hashlib.md5(relative.encode()).digest())
        result = self.fetch()
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]['content'], later)

    def test_wal_failure_cannot_be_cached_as_a_success_on_retry(self):
        updated = self.message_plain('updated', [self.long_text, '#接龙\n【富国基金】\n1. 赵阳'])
        relative = 'message/message_0.db'
        encrypted_wal(updated, Path(str(self.db_dir/relative)+'-wal'), self.key, hashlib.md5(relative.encode()).digest())
        with patch('wechat_native.messages.apply_wal', side_effect=OSError('fixture WAL error')):
            with self.assertRaisesRegex(ValueError, '日志合并失败'):
                self.fetch()
        self.assertEqual(len(self.fetch()), 2)

    def test_missing_message_key_is_error_not_empty_success(self):
        self.keys.pop(hashlib.md5(b'message/message_0.db').hexdigest())
        self.save_config()
        with self.assertRaisesRegex(ValueError, '凭据未覆盖'):
            self.fetch()

    def test_unused_contact_search_index_does_not_block_message_read(self):
        index = plaintext_database(self.root/'plain_contact_fts.db', [
            'CREATE TABLE search_index (content TEXT)'])
        self.add_db('contact/contact_fts.db', index)
        self.keys.pop(hashlib.md5(b'contact/contact_fts.db').hexdigest())
        self.save_config()
        self.assertEqual(self.fetch()[0]['content'], self.long_text)

    def test_wrong_key_is_detected_before_query(self):
        self.keys[hashlib.md5(b'message/message_0.db').hexdigest()] = 'ab' * 32
        self.save_config()
        with self.assertRaisesRegex(ValueError, '无法解密'):
            self.fetch()

    def test_wrong_group_is_error(self):
        with self.assertRaisesRegex(ValueError, '0 个会话'):
            self.source.fetch(parse_bjt('2026-10-26'), parse_bjt('2026-10-26 09:00:00'), '其他群')

    def test_complete_encrypted_source_to_workbook(self):
        roster, target = self.root/'roster.xlsx', self.root/'target.xlsx'
        make_roster(roster, ['赵阳', '张建民'])
        make_target(target, ['赵阳', '张建民'])
        result = update_file(target, roster, self.source.fetch, first_start='2026-10-26', end='2026-10-26 09:00:00')
        self.assertEqual(result['cleared_count'], 2)
        self.assertEqual(result['issues'], [])

    @unittest.skipUnless(sys.platform == 'darwin', 'Mac helper only')
    def test_upstream_sources_have_only_recorded_changes(self):
        root = Path(__file__).resolve().parents[1] / 'vendor'
        manifest = json.loads((root / 'UPSTREAM.json').read_text('utf-8'))
        changes = {item['file']: item['sha256'] for item in manifest['changes']}
        self.assertEqual(set(changes), {'scanner_mac.py', '_mac_lldb_capture.py', 'messages.py'})
        for name, digest in manifest['files_sha256'].items():
            self.assertEqual(hashlib.sha256((root/'wechat_native'/name).read_bytes()).hexdigest(), changes.get(name, digest), name)

    @unittest.skipUnless(sys.platform == 'darwin', 'Mac helper only')
    def test_helper_arguments_preserve_spaces_quotes_and_shell_characters(self):
        from wechat_native import scanner_mac
        helper = Path("/tmp/tool space's/$literal/_mac_helper.py")
        db_dir = '/tmp/account space/"quoted"/$(literal)/db_storage'
        with patch.object(scanner_mac, '_helper_path', return_value=helper), \
             patch.object(Path, 'exists', return_value=True), \
             patch.object(scanner_mac, '_osascript_run_with_admin', return_value=(0, '{"keys": {}}', '')) as run:
            self.assertEqual(scanner_mac.extract_keys(db_dir), {})
        self.assertEqual(shlex.split(run.call_args.args[0]), [
            '/usr/bin/python3', str(helper), '--db-dir', db_dir, '--limit-mb', '600',
        ])
        self.assertEqual(run.call_args.kwargs, {'timeout': 300})


if __name__ == '__main__':
    unittest.main()
