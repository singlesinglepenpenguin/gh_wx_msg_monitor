"""Check naming rules on disk and inside retained ZIP archives."""
from pathlib import Path
import json
import re
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
CHINESE = re.compile(r'[\u3400-\u9fff]')
EXCEL = {'.xlsx', '.xls', '.xlsm'}


def invalid_name(name, is_directory):
    return bool(CHINESE.search(name)) and (is_directory or Path(name).suffix.lower() not in EXCEL)


def main():
    invalid = []
    for path in ROOT.rglob('*'):
        if invalid_name(path.name, path.is_dir()):
            invalid.append(str(path.relative_to(ROOT)))
    for archive in (ROOT/'data/diagnostics').glob('*.zip'):
        with zipfile.ZipFile(archive) as source:
            for item in source.infolist():
                parts = Path(item.filename).parts
                if any(invalid_name(name, index < len(parts)-1 or item.is_dir()) for index, name in enumerate(parts)):
                    invalid.append(str(archive.relative_to(ROOT)) + ':' + item.filename)
    print(json.dumps({'invalid_names': invalid, 'status': 'ok' if not invalid else 'failed'}, ensure_ascii=False, indent=2))
    return bool(invalid)


if __name__ == '__main__':
    sys.exit(main())
