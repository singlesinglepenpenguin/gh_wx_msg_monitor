"""Real Windows acceptance. No substitute keys/messages; Excel uses an isolated copy."""
from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from group_msg_monitor.error_messages import record_error, record_runtime_log, redact
from group_msg_monitor.relay_core import BJT, file_hash, summarize_issues, update_file
from group_msg_monitor.wechat_source import WeChatSource, initialize


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--initialize', action='store_true', help='Explicit one-time upstream initialization')
    parser.add_argument('--db-dir')
    parser.add_argument('--start', default='2026-10-26 00:00:00')
    parser.add_argument('--group', default='GH派点反馈群')
    args = parser.parse_args()
    local = ROOT / 'data/local_data'
    report_path = ROOT / 'data/diagnostics/windows_live_validation.json'
    report = {'started_at': datetime.now(BJT).isoformat(), 'synthetic': False,
              'initialization_requested': args.initialize, 'live_message_read': False,
              'real_excel_update': False, 'stage': 'start'}

    def save():
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

    def log(line):
        safe = redact(line)
        print(safe, flush=True)
        record_runtime_log(local, safe + '\n')

    try:
        save()
        if args.initialize:
            report['stage'] = 'one_time_initialization'
            save()
            report['credential_count'] = initialize(args.db_dir, local, log)
            save()
        source = WeChatSource(local, log)
        report['stage'] = 'live_group_inspection'
        save()
        inspected = source.inspect(args.group)
        report['group_matches'] = len(inspected['matched_sessions'])
        report['session_count'] = inspected['session_count']
        report['stage'] = 'live_message_query'
        save()
        end = datetime.now(BJT)
        start = datetime.strptime(args.start, '%Y-%m-%d %H:%M:%S').replace(tzinfo=BJT)
        messages = source.fetch(start, end, args.group)
        report.update(live_message_read=True, message_count=len(messages),
                      maximum_content_length=max((len(m.get('content', '')) for m in messages), default=0))
        if not messages:
            raise ValueError('LIVE_VALIDATION_EMPTY: no real relay messages in acceptance interval')
        report['stage'] = 'real_excel_update_on_copy'
        save()
        stamp = datetime.now(BJT).strftime('%Y%m%d_%H%M%S')
        work = ROOT / 'data/validation' / stamp
        work.mkdir(parents=True, exist_ok=False)
        original = ROOT / 'data/reference/要分名单 26Q3.xlsx'
        roster = ROOT / 'data/reference/研究所内部通讯录9.10.xlsx'
        original_hash = file_hash(original)
        roster_hash = file_hash(roster)
        target = work / 'baseline.xlsx'
        shutil.copy2(original, target)
        result = update_file(target, roster, source.fetch, first_start=args.start,
                             end=end.strftime('%Y-%m-%d %H:%M:%S'), group=args.group,
                             save_as_new=True, output_dir=work / 'output', records_dir=work / 'records')
        assert file_hash(original) == original_hash
        assert file_hash(roster) == roster_hash
        assert Path(result['output']).is_file()
        report.update(real_excel_update=True, output=result['output'],
                      notes_file=result['notes_file'], cleared_count=result['cleared_count'],
                      issue_count=len(result['issues']), issues_to_review=len(summarize_issues(result['issues'])),
                      reference_files_unchanged=True,
                      stage='complete', status='passed')
        log('真实 Windows 群读取及 Excel 副本更新通过。')
    except Exception as exc:
        failure = record_error(local, exc, 'Windows real acceptance: ' + report['stage'])
        report.update(status='failed', error_code=failure['code'], error=redact(exc))
        log(f"错误编号 {failure['code']}：{failure['summary']} 处理办法：{failure['action']}")
        raise
    finally:
        report['finished_at'] = datetime.now(BJT).isoformat()
        save()


if __name__ == '__main__':
    main()
