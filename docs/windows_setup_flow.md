# Windows 两个入口与无密钥验收

客户交付物：`dist/GroupMessageMonitor_windows.zip`。完整解压，首次点 `setup.bat`，以后点 `start.bat`。Python、Tk、加解密依赖及全部 gh_ui 上游脚本都在包内；不需要 AI、命令行或手工密钥。

## 客户流程

1. `setup.bat` 检查完整包，补齐系统 PowerShell 的进程查找路径，打开“微信连接设置”。
2. 自行打开微信并完成本人登录，再点“识别当前账号”。工具通过 Windows Restart Manager 查询已知数据库文件的占用进程，只有微信实际打开的目录才标为当前账号，不按排序或修改时间猜选。
3. 点“自动获取密钥并连接微信”，确认一次性连接。工具通过 Windows 的正常关闭请求让微信保存并退出；不强制结束进程。检测退出后调用未改动触发流程的上游 active-first/raw-fallback 初始化。若微信拒绝退出，向导提示从微信菜单退出，自动等待后继续。
4. 系统 UAC 及微信本人登录由本人确认。凭据保存后立即核验目标群，再正常打开微信用于日常同步；不再次取钥。
5. 返回主界面，在 GUI 选择通讯录、更新基准及首次时间，点击更新。勾选自动更新后会保存该选择，后续 `start.bat` 接续文件、进度和定时设置。

系统正常关闭的依据见 [Microsoft Restart Manager](https://learn.microsoft.com/en-us/windows/win32/api/restartmanager/nf-restartmanager-rmshutdown)。本工具只注册已识别的微信主进程，请求参数为 `0`，没有 `RmForceShutdown`、`TerminateProcess`、`taskkill` 或应用重启 API；返回码不能替代最终的微信进程状态检查。Windows 元数据查询和退出引导均属于工具界面准备工作，不读取密钥或消息。实际密钥获取、解密和群查询仍复用 vendor 上游。

## 实际冷启动结果，2026-10-30

- 同一台 Windows、全新解压目录，初始无工具配置、密钥或解密缓存；源项目密钥已移出运行位置，旧备份没有复制到测试包。环境 PATH 移除系统 Python。不是新装 Windows 或另一台电脑的验证。
- 首轮冷测试发现：裁减后的 PATH 不含 PowerShell，GUI 报 `WIN_ACTIVE_FAILED: No script transcript was produced`，没有生成凭据。两个 bat 现已显式补齐系统 PowerShell 目录，重新解压后通过。
- 从 `setup.bat` 打开 GUI，真实点击识别和连接按钮，无命令行初始化、无手工密钥、无外部配置导入。微信正常退出后自动继续；在客户端自动更新到 **4.1.15.56** 后，取钥及 **32/32** 数据库 HMAC 校验通过，目标群“GH派点反馈群”核验通过。
- 关闭后以 `start.bat` 重启，复用新凭据；日志中的初始化次数仍为 **1**。当天查询读取 **5 条真实接龙**，另存 Excel 和 TXT 成功；原参考表缺少对应机构，清空 **0 格**，合并为 **3 项待核对**。不能把此轮报告成实际清空 29 格，29 格属于此前另一时段的真实验收。
- 最终 GUI 版本实测保存自动更新勾选，关闭再用 `start.bat` 打开后保持勾选和已连接状态；没有再次取钥。
- 原始参考表与更新前备份哈希一致。交付 ZIP 不含运行 data、密钥、聊天缓存或任何 Excel；客户自己在 GUI 选择业务表。
- 72 项回归测试无失败，2 项平台测试跳过；vendor 指纹核验无未记录差异。

证据：`data/diagnostics/cold_setup_validation.json`、`cold_setup_success.png`、`cold_start_monitoring_success.png`。首次失败的完整日志留在独立的首轮目录，未覆盖为成功。`scripts/record_cold_setup.py` 只核验并记录已完成的 GUI 流程，不执行密钥初始化。

## 已验证边界

客户仍须安装并登录官方微信，确认 Windows/微信自己的授权，并提供其实际要处理的业务 Excel；这些均通过正常 GUI 完成。工具不能代替本人认证。自定义安装位置、客户端版本变化和不同机器仍以其实际初始化结果为准；错误编号及处理办法落在本机日志。

源码开发分支的 setup 仍安装 `.venv` 依赖，需要已有 Python；客户使用的完整 Windows 交付包完全不依赖这条分支。
