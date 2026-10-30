# 本项目的用户约束

- 微信连接、初始化、密钥获取、解密、群定位与消息查询复用本机 `gh_ui/api/wechat_native`；保持原有触发方式、授权和采集行为。
- 允许修复路径传参等兼容性错误，须记录差异；不改变上游连接触发流程，不使用 monkey patch 或替代采集方式。
- 原流程遇到问题时，记录真实错误，按 gh_ui 原有步骤测试；不得为绕过问题改变微信采集行为。
- 允许开发本工具自己的界面、打包、通讯录匹配和 Excel 更新业务。打包必须包含上游所需依赖。
- `vendor/UPSTREAM.json` 保留上游 SHA256，并记录已授权补丁及其 SHA256；交付前确认只有已记录差异。
- 构造数据测试不能报告为真实微信读取成功。
- 新遇到的卡点须更新 `data/diagnostics/error_message.md`：记录原始错误、出错阶段、已证实原因、尚未确认的推测、处理步骤和验证结果；对应用户可处理的错误同步维护 `src/group_msg_monitor/error_messages.py`。
- 工具运行故障记录到 `data/local_data/error_message.jsonl`，报错中显示错误编号和处理办法；不得记录完整候选密钥或让记录失败覆盖原始错误。
- 向用户明确区分一次性密钥初始化与日常读取；已有有效凭据时微信可以持续保持登录，不得把调试器作为常驻监听或每次读取自动重复抓取。

- 所有目录及非 Excel 文件名使用英文；Excel 文件名允许中文。业务源码放 src/group_msg_monitor，运行数据放 data。
- 清理连接、缓存或恢复首次使用状态时，必须保留 `reference/` 和 `data/reference/` 中的用户测试 Excel，不得作为缓存删除。
- 客户包只能使用 `docs/diagnostics/error_message.md` 通用说明，不得打包 `data/diagnostics/error_message.md` 开发诊断历史；交付前运行 `scripts/verify_distribution.py`，同时检查文档中的账号标识与本机路径。
- 自动任务不得依赖模态报错窗口结束；验证调度器、后台线程、结果队列、Excel 版本和失败恢复的完整链路，明确区分模拟时间/构造消息与真实微信验证。
- 继续开发前阅读 `docs/maintenance_review.md` 中的接续摘要；保留已认可版本的提交与推送记录，功能修复、无行为变化清理独立提交。
