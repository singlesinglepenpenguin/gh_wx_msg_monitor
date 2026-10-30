# Operator guide

完整终端用户步骤见 [user_manual.md](user_manual.md)（Windows 分发用，无 AI 也可按手册独立操作）。

1. macOS 双击根目录 start.command；Windows 双击 start.bat（首次会自动安装依赖，也可单独运行 setup.bat 重装）。日常微信正常保持登录。Windows 首次在“微信连接设置”选择账号，从托盘退出微信，再点“自动获取密钥并连接微信”，按 gh_ui 原流程允许授权并在新窗口登录一次。
2. 选择内部通讯录和要更新的 Excel，核对基准文件、上次更新时间和本次截止时间（北京时间）。
3. 点击“开始更新”。新表保存在 data/output，旧基准不覆盖；成功后自动切换到新表供下次接续。
4. “打开最新结果”打开最新生成表；“查看最新记录”打开 data/records/update_YYYYMMDD_HHMMSS.txt（一轮一份）；“打开结果文件夹”打开 data/output。

每轮明细包括：机构、姓名、单元格、删除前内容、接龙时间、发送人、原文。实际删除、原本已空、正常跳过、待核对分开列出；原本已空不能当作本次删除。每轮 JSON/文字说明/备份在 data/records/<run_id>，每轮 TXT 直接放在 data/records/update_YYYYMMDD_HHMMSS.txt，时间与 Excel 一致，不再生成总汇。

换 Excel 只要重新选择基准。没有进度的新表需填写首次开始时间；有进度的表自动接续。无需另建文件夹。Excel 可用中文名，其余文件和目录使用英文。

首次使用或更换微信账号，在“微信连接设置”核对自动发现的数据目录，未找到时手动选择存储位置、账号文件夹或 db_storage。已有凭据的日常更新无需抓取密钥。

高级维护保留 gh_ui 的普通扫描、一次性新版抓取及签名修复。新版抓取可能触发微信安全验证，签名修复会替换微信签名并可能影响自动更新；只按实际错误使用。当前可读取时工具阻止重复初始化或签名修改。

自动更新需要工具保持运行，按北京时间每天 09:00、15:00 执行；关闭工具后不会作为后台服务运行。

有问题查 data/diagnostics：README.txt 为索引，error_message.md 为手册，runtime.log 为阶段日志，error_message.jsonl 为故障与处理建议。不要向他人发送 data/local_data 中的微信凭据。
