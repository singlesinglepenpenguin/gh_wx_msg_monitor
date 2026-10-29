Diagnostics index

error_message.md       排错手册与历史卡点
runtime.log            自动记录的运行阶段
error_message.jsonl    脱敏故障、错误编号、建议；原始故障同步保留在 data/local_data
latest_update.json     最近一次完整更新明细
verification.md        验证记录，区分实机/夹具/回放
layout_migration.json  本轮目录映射、Excel/进度/凭据保留校验
history_evidence.zip   原始历史验证证据，条目名称已改为英文
history_workbooks.zip  旧实测工作目录，Excel 内容保留
history_evidence.zip/retired_scripts  已退休的一次性实测脚本
naming_check.json      磁盘及 ZIP 条目名称检查
layout_live_check.json  新结构真实微信临时副本验证

旧版构建、清理、固定输出和维护检查都在 history_evidence.zip/previous_checks，不再散放。
tests.log              最新自动化检查
runtime_check.json     源码运行依赖检查
wechat_inspect.json    真实微信只读连接检查
main_window.png / wechat_settings.png / maintenance.png  本工具窗口实拍

日常结果在 data/output；完整更新 TXT 在 data/records/update_YYYYMMDD_HHMMSS.txt，逐轮备份与明细在 data/records。文件夹及非 Excel 文件名均使用英文。

日志与明细可能包含姓名、群名和个人目录，按需提供排错文件；不要提供 data/local_data 中的密钥和缓存。
