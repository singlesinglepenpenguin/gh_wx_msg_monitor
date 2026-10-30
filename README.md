# Group Message Monitor

读取微信群接龙，按机构和姓名清空 Excel 对应单元格，每轮另存带北京时间的新版本。

macOS 源码首次使用：安装带 Tkinter 的 Python 3.10 或更新版本，双击 **setup.command** 安装依赖（需要联网），成功后双击 **start.command**。以后只需 start.command。

Windows 源码使用：安装带 Tkinter 的 Python 3.10 或更新版本后，**日常只需双击 start.bat**。首次若尚无 `.venv`，`start.bat` 会自动调用 `setup.bat` 联网安装依赖（`requirements.txt`：openpyxl / pycryptodome / zstandard；PyPI 失败会改用清华镜像）。也可单独双击 `setup.bat` 重装环境，对应 macOS 的 `setup.command`。初始化微信密钥若提示拒绝访问进程，请以管理员身份运行 `start.bat`。当前源码分发入口为 `.bat`（与 macOS `.command` 对等）；`scripts/package_app.py` 可在目标机打成 `GroupMessageMonitor.exe`，本轮以源码 bat 入口交付。

请将完整项目放在自己可写的文件夹，不能只复制 src 或安装普通 wheel；上游代码及辅助脚本在 vendor。

在新电脑选择自己的通讯录和更新基准，再按“微信连接设置”完成该电脑的一次性初始化。不会随源码提供原用户的表格、微信凭据或历史配置。已有有效凭据时微信保持登录即可。当前不重新打包。

## Directory layout

```text
src/group_msg_monitor/   业务源码与模块入口
scripts/                构建、验证、截图和报告脚本
tests/                 回归测试
docs/                  运营说明和工程说明
vendor/                gh_ui 原生依赖、上游与补丁指纹
data/
   reference/           两份原始参考 Excel
   output/              所有生成的新 Excel、隐藏进度
   records/             每轮明细、备份、update_YYYYMMDD_HHMMSS.txt
   local_data/          本机凭据、偏好、可再生缓存
   diagnostics/         排错手册、日志与历史压缩档
start.command / start.bat   日常启动入口（macOS / Windows）
setup.command / setup.bat   首次安装源码环境（macOS / Windows）
pyproject.toml          Python 包配置
requirements.txt        运行依赖版本
```

Excel 文件名可以是中文；其他文件名及所有文件夹名使用英文。隐藏进度和锁使用英文哈希名，不复制 Excel 的中文名称。

## Usage

选择通讯录和一个更新基准，核对读取时间，点击“开始更新”。结果固定保存到 data/output；生成后自动接续新表。更换 Excel 只需重新选择基准，无需建立结果子文件夹。

“打开最新结果”打开最新生成的 Excel；“查看最新记录”打开 data/records/update_YYYYMMDD_HHMMSS.txt，只包含该轮机构、姓名、坐标及接龙证据；“打开结果文件夹”固定打开 data/output。

缺少姓名、缺人员列、提示语正常跳过；同名歧义和机构问题保留核对。自动更新仅在工具运行时按北京时间 09:00、15:00 执行。

## Development

```sh
# macOS
PYTHONPATH=src .venv/bin/python -m group_msg_monitor
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests
.venv/bin/python scripts/check_layout.py

# Windows (PowerShell)
$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m group_msg_monitor
$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m unittest discover -s tests
.\.venv\Scripts\python.exe scripts\check_layout.py
```

脚本：scripts/capture_ui.py 只截图本工具窗口；scripts/export_report.py 导出当前报告；scripts/verify_reference.py 使用参考表副本做验证；scripts/package_app.py 留待目标系统打包，本轮未运行。未来可分发的打包文件名为 GroupMessageMonitor。

微信连接、授权、初始化、解密、群定位和查询沿用 vendor/wechat_native，保留 vendor/UPSTREAM.json 中的已记录补丁。Windows 自动取钥按 gh_ui 的 active-first、raw-fallback 顺序执行，不使用自写附加脚本。首次选择账号目录后，从托盘退出微信，点“自动获取密钥并连接微信”并在新窗口完成登录；日常有凭据时无需再次取钥。data/local_data 含个人密钥和缓存，不随应用分发。

新环境验收：`python3 scripts/verify_clean_environment.py` 从 Git 索引复制源码至临时目录，用独立虚拟环境安装依赖，检查首次启动并运行回归测试。报告保存在 data/diagnostics/clean_environment.json。此检查不连接真实微信，不代表另一台电脑已完成微信授权；Windows 尚需目标电脑实测。

本轮结果及验收边界见 [environment validation](docs/environment_validation.md)。

详见 [operator guide](docs/operator_guide.md)、[architecture](docs/architecture.md)、[troubleshooting](data/diagnostics/README.txt)。
