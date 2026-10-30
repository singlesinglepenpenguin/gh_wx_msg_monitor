"""Verify every bundled upstream file against its original or recorded patch hash."""
from pathlib import Path
import argparse
import hashlib
import json

ROOT = Path(__file__).resolve().parents[1]


def verify(vendor_root=None):
    vendor_root = Path(vendor_root) if vendor_root else ROOT / 'vendor'
    manifest = json.loads((vendor_root / 'UPSTREAM.json').read_text('utf-8'))
    originals = manifest['files_sha256']
    patches = {item['file']: item['sha256'] for item in manifest['changes']}
    base = vendor_root / 'wechat_native'
    actual = {p.relative_to(base).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in base.rglob('*') if p.is_file() and '__pycache__' not in p.parts}
    expected = {**originals, **patches}
    errors = [name for name in sorted(set(actual) | set(expected))
              if actual.get(name) != expected.get(name)]
    return {'status': 'failed' if errors else 'ok', 'files': len(actual), 'differences': errors}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--vendor-root', type=Path)
    result = verify(parser.parse_args().vendor_root)
    print(json.dumps(result, indent=2))
    raise SystemExit(result['status'] != 'ok')
