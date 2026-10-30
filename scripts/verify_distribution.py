"""Reject private runtime files and account-specific customer documentation."""
from pathlib import Path, PurePosixPath
import argparse
import json
import re
import zipfile


def verify(archive):
    errors = []
    private_names = {'wechat.json', 'all_keys.json', 'preferences.json', 'session_names.json', '.env'}
    private_parts = {'reference', 'local_data', 'decrypted', 'records', 'output', '.git'}
    private_suffixes = {'.db', '.xlsx', '.xls', '.log', '.jsonl', '.dmp'}
    with zipfile.ZipFile(archive) as bundle:
        if bundle.testzip():
            errors.append('ZIP integrity check failed')
        for name in bundle.namelist():
            path = PurePosixPath(name)
            if (name.startswith('GroupMessageMonitor/data/') or path.name in private_names
                    or set(path.parts) & private_parts or path.suffix.lower() in private_suffixes):
                errors.append('Private runtime file: ' + name)
            if path.suffix.lower() == '.md':
                text = bundle.read(name).decode('utf-8')
                if re.search(r'wxid_[A-Za-z0-9_]+|/Users/[^/\s]+|(?<![A-Za-z])\b[A-Za-z]:[\\/]', text):
                    errors.append('Account or local path in documentation: ' + name)
    return {'status': 'failed' if errors else 'passed', 'errors': errors}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive', type=Path)
    result = verify(parser.parse_args().archive)
    print(json.dumps(result))
    raise SystemExit(result['status'] != 'passed')
