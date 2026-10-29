"""在目标平台打包；Mac 生成 .app，Windows 生成 .exe。"""
import os
from pathlib import Path
import subprocess
import sys

root = Path(__file__).resolve().parents[1]
dist_dir = Path(os.environ.get('GROUP_MSG_MONITOR_BUILD_DIST', root / 'dist')).resolve()
separator = os.pathsep
build_env = os.environ.copy()
build_env['PYINSTALLER_CONFIG_DIR'] = str(root / '.work/pyinstaller-cache')
subprocess.run([sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean', '--windowed',
                '--distpath', str(dist_dir),
                '--name', 'GroupMessageMonitor', '--add-data', f'{root / "vendor"}{separator}vendor',
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
