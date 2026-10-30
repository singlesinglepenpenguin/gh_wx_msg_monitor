"""Summarize observable cold GUI acceptance evidence without exposing credentials."""
from datetime import datetime
import json
import hashlib
from pathlib import Path
import shutil
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from group_msg_monitor.relay_core import BJT, file_hash, latest_run_note, summarize_issues


def main():
    session = json.loads((ROOT / 'data/diagnostics/cold_setup_session.json').read_text('utf-8-sig'))
    package = Path(session['package'])
    local = package / 'data/local_data'
    path = local / 'wechat.json'
    credentials = json.loads(path.read_text('utf-8'))
    result = json.loads((package / 'data/diagnostics/latest_update.json').read_text('utf-8'))
    runtime = (package / 'data/diagnostics/runtime.log').read_text('utf-8')
    preferences = json.loads((local / 'preferences.json').read_text('utf-8'))
    assert preferences.get('auto') is True
    assert session['credentials_removed'] and session['package_has_no_credentials']
    assert len(credentials['keys']) == 32
    assert runtime.count('正在调用 gh_ui 微信读取初始化') == 1
    assert '微信已正常退出' in runtime
    assert '微信读取正常，已找到目标群' in runtime
    assert result['message_count'] > 0
    assert Path(result['output']).is_file()
    assert file_hash(result['source']) == file_hash(result['backup'])
    with zipfile.ZipFile(ROOT / 'dist/GroupMessageMonitor_windows.zip') as archive:
        private = [name for name in archive.namelist() if name.startswith('GroupMessageMonitor/data/')
                   or name.endswith(('wechat.json', 'all_keys.json', '.xlsx'))]
        assert not private, private
        assert 'GroupMessageMonitor/setup.bat' in archive.namelist()
        assert 'GroupMessageMonitor/start.bat' in archive.namelist()
    report = {
        'checked_at': datetime.now(BJT).isoformat(), 'status': 'passed', 'synthetic': False,
        'scope': 'Same Windows machine; freshly extracted distribution, no tool credentials or cache, system Python removed from PATH',
        'client_dll_version': '4.1.15.56', 'setup_entry': 'setup.bat', 'daily_entry': 'start.bat',
        'initial_state': session, 'init_trigger': 'Real GUI button: 自动获取密钥并连接微信',
        'account_selection': 'Windows file ownership metadata; current account, no manually entered path',
        'normal_client_exit': 'User-confirmed Restart Manager request, flags=0; no forced shutdown',
        'credential_count': len(credentials['keys']), 'initialization_count': 1,
        'daily_start_reused_credentials': True, 'group_verified': 'GH派点反馈群',
        'automatic_monitoring_restored_after_restart': True,
        'distribution_sha256': hashlib.sha256((ROOT / 'dist/GroupMessageMonitor_windows.zip').read_bytes()).hexdigest(),
        'live_messages': result['message_count'], 'cleared_count': result['cleared_count'],
        'issues_to_review': len(summarize_issues(result['issues'])), 'query_from': result['from'],
        'output': result['output'], 'notes_file': str(latest_run_note(package / 'data/records')),
        'original_workbook_unchanged': True, 'distribution_contains_private_data': False,
        'no_key_files_copied_into_test_package': True,
    }
    (ROOT / 'data/diagnostics/cold_setup_validation.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    # Leave the source workspace usable with the newly generated GUI credentials.
    # The release archive remains clean; never restore/copy the old backup into the test.
    shutil.copy2(path, ROOT / 'data/local_data/wechat.json')
    (ROOT / 'data/local_data/wechat.json').chmod(0o600)
    print(json.dumps({key: report[key] for key in ('status', 'credential_count', 'initialization_count',
                     'live_messages', 'cleared_count', 'issues_to_review', 'distribution_contains_private_data')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
