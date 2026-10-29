# Clean environment validation

验证日期：2026-10-29（北京时间）。验证方式：同一台 macOS 的临时源码副本、空 HOME、独立 venv；不是另一台电脑或虚拟机。

复现命令：`python3 scripts/verify_clean_environment.py`。需先将源码加入 Git 索引。临时目录和 HOME 名称均带空格，检查路径传参；临时副本只包含索引中的源码、文档、测试、vendor 和排错手册，不包含参考表、凭据、配置、缓存、更新结果或原有 .venv。结束自动删除临时环境。

已验证：

- setup.command 从头创建不继承系统 site-packages 的 Python 3.12 环境，并通过网络安装 requirements.txt。
- pip check 无依赖冲突，运行依赖、上游辅助脚本及排错手册齐全。
- 57 项回归测试通过，包括生成的加密数据库、解密及完整接龙读取、匹配、Excel 更新、逐轮同时间戳 TXT、连续版本和上游指纹检查。加密数据库为测试构造数据。
- 空 HOME 首次打开真实 Tk 界面，文件选择为空，缺少凭据时不崩溃；微信连接设置可打开，不自动执行初始化，不产生微信密钥。
- 现有生产表格和微信凭据不参与此验证。

首次使用发现并修复：原 start.command 直接使用已存在的 .venv，新源码缺少安装入口。现已提供 setup.command；未安装时 start.command 会给出明确提示。

验证边界：新电脑仍需自行安装带 Tkinter 的 Python 3.10 或更新版本，将整个源码放在可写目录，选择自己的两份 Excel，并完成该电脑的微信权限与一次性密钥初始化。空环境测试不证明任意微信版本均可初始化，也不证明另一台电脑的系统权限已获批准。未测试 Windows 实机，未制作打包产物。微信触发、授权与采集方式未修改。

逐项日志和机器可读结果位于 data/diagnostics/clean_*.log、clean_environment.json，均不提交到 Git。
