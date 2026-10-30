"""在目标平台打包；Mac 生成 .app，Windows 生成 .exe。"""
import os
from pathlib import Path
import subprocess
import sys
import shutil
import hashlib
import zipfile

root = Path(__file__).resolve().parents[1]
subprocess.run([sys.executable, str(root / 'scripts/verify_vendor.py')], cwd=root, check=True)
source_files = list((root / 'src').rglob('*.py')) + [root / 'scripts/launch_app.py']
source_hashes = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in source_files}
dist_dir = Path(os.environ.get('GROUP_MSG_MONITOR_BUILD_DIST', root / 'dist')).resolve()
separator = os.pathsep
build_env = os.environ.copy()
build_env['PYINSTALLER_CONFIG_DIR'] = str(root / '.work/pyinstaller-cache')
vendor_args = []
for source in sorted((root / 'vendor').rglob('*')):
    if source.is_file() and '__pycache__' not in source.parts:
        destination = Path('vendor') / source.relative_to(root / 'vendor').parent
        vendor_args.extend(['--add-data', f'{source}{separator}{destination}'])
subprocess.run([sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean', '--windowed',
                '--distpath', str(dist_dir),
                '--name', 'GroupMessageMonitor', *vendor_args,
                '--add-data', f'{root / "data/diagnostics/error_message.md"}{separator}data/diagnostics',
                '--paths', str(root / 'vendor'),
                '--paths', str(root / 'src'),
                '--hidden-import', 'wechat_native.scanner_mac',
                '--hidden-import', 'wechat_native.scanner_win',
                '--hidden-import', 'wechat_native.active_key',
                '--hidden-import', 'Crypto.Cipher.AES', '--hidden-import', 'zstandard',
                '--collect-all', 'openpyxl', str(root / 'scripts/launch_app.py')], cwd=root, env=build_env, check=True)
if sys.platform == 'darwin':
    executable = dist_dir / 'GroupMessageMonitor.app/Contents/MacOS/GroupMessageMonitor'
elif os.name == 'nt':
    executable = dist_dir / 'GroupMessageMonitor/GroupMessageMonitor.exe'
else:
    executable = dist_dir / 'GroupMessageMonitor/GroupMessageMonitor'
# 直接测试交付的二进制，而不是开发环境；缺少延迟导入时让打包失败。
subprocess.run([str(executable), '--check-runtime'], cwd=root, check=True, timeout=60)
if any(hashlib.sha256(p.read_bytes()).hexdigest() != digest for p, digest in source_hashes.items()):
    raise RuntimeError('Source files changed during packaging; rebuild before delivery.')
if os.name == 'nt':
    subprocess.run([sys.executable, str(root / 'scripts/verify_vendor.py'),
                    '--vendor-root', str(executable.parent / '_internal/vendor')], cwd=root, check=True)
    shutil.copy2(root / 'start.bat', executable.parent / 'start.bat')
    shutil.copy2(root / 'setup.bat', executable.parent / 'setup.bat')
    shutil.copy2(root / 'docs/user_manual.md', executable.parent / 'user_manual.md')
    archive = dist_dir / 'GroupMessageMonitor_windows.zip'
    with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED) as bundle:
        for item in sorted(executable.parent.rglob('*')):
            relative = item.relative_to(executable.parent)
            if item.is_file() and relative.parts[0] != 'data' and '__pycache__' not in relative.parts:
                bundle.write(item, Path('GroupMessageMonitor', relative).as_posix())
    print(f'Windows package: {archive}')
