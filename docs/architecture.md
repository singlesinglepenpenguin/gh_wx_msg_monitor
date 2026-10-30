# Architecture

## Modules

| Module | Responsibility |
| --- | --- |
| group_msg_monitor.app | Tkinter 界面、任务队列、定时调度、CLI |
| group_msg_monitor.relay_core | 表格解析、完整姓名匹配、清空计划、仅修改目标 worksheet、版本和记录 |
| group_msg_monitor.institution_match | 消息主语证据与 Excel 机构身份解析，明确区分 matched / missing / ambiguous / unrecognized |
| group_msg_monitor.wechat_source | 调用 gh_ui 原生实现，凭据验证、缓存刷新、群定位和完整接龙读取 |
| group_msg_monitor.error_messages | 错误编号、建议、脱敏与自动日志 |
| group_msg_monitor.paths | 集中定义源码/打包资源与运行目录 |

入口为 python -m group_msg_monitor；start.command 设置源码路径并使用本地 .venv。scripts/launch_app.py 是未来 PyInstaller 入口。源码使用包内相对导入，测试通过 PYTHONPATH=src 运行。

## Processing

界面选择基准与通讯录 → 确认进度或首次开始时间 → 原生刷新数据库及 WAL → 精确定位 GH派点反馈群 → 按北京时间窗口读取完整接龙 → 合并待核对历史 → 定位机构/完整姓名/列 → 生成单元格计划 → 备份 → 仅删除命中格公式/值 → 保存新版本、进度、审计 → 自动选择新表。

只清空内容，保留样式及其他工作表。姓名后表情可作边界，HTML 实体解码；不拼接姓名中间的表情。缺姓名/缺列/提示语正常跳过，不作为待核对。机构问题及同名歧义保留原始证据重试。

机构判断分为消息角色、身份核对、更新许可三个步骤。每轮重新读取 Excel 机构词表；只接受消息主语位置或明确的“机构：”字段作为归属证据，正文提及不参与。括号本身不表示机构；机构名称须对应词表身份或有组织名称证据。未识别的格式保留原文，不做模糊猜测。

多个主体或字段冲突时整条接龙暂停，即使 Excel 只包含其中一家也不得自动缩减为单一候选。只有明确的单一主语且对应唯一机构行时才允许生成单元格计划。每条 evidence 保存 institution_resolution（状态、候选、未知主体、原文作用域、证据角色）；TXT 同步显示本轮实际机构列。支持范围以独立解析器测试及回放结果为准，不承诺理解任意自然语言。

## Data and naming

所有运行数据集中在 data。output 仅放结果和隐藏进度；records/<run_id> 包含 details.json、更新前备份和该轮归档。data/records/update_YYYYMMDD_HHMMSS.txt 一轮一份，时间与对应 Excel 一致；按钮只打开最新一轮。diagnostics 保存故障、验证报告和历史压缩档；local_data 保存本机密钥/偏好/解密缓存；reference 为原始表。

Excel 名称允许中文。进度名为 .relay-state-<filename_hash>.json，保存 output_name、through、group、pending、last_archive、input_source 和输入指纹；锁名为 .relay-lock-<filename_hash>.lock。改目录时只修正路径引用，保留业务时间、待处理消息和所有 Excel 字节。旧偏好通过 local_data/legacy_paths.json 映射；旧命名进度仅兼容读取，不再创建。

latest_version 通过输入路径与输入指纹追踪版本，手工修改过输入时不强制跳转。每次另存新版本，失败不推进进度；同秒文件名冲突增加序号。

## Native boundary

vendor/wechat_native 沿用本机 gh_ui；SHA256 及三项已授权差异在 vendor/UPSTREAM.json。没有运行时 monkey patch 或替代采集。已有凭据时不自动附加调试器，也不为日常更新重复抓取。

paths.py 区分资源位置和可写运行目录；Mac .app 从包内读取资源，在 .app 同级 data 写数据。打包脚本只收 vendor、源码依赖和排错手册，不收个人数据。本轮没有构建安装包；Windows 尚未实机验证。

## Verification

自动化测试覆盖匹配边界、加密库/WAL、完整接龙、幂等、失败不推进、版本接续、固定输出、记录、目录迁移和维护封装。构造夹具、历史回放、真实微信查询分别记录，不互相冒充。data/diagnostics/layout_migration.json 记录本轮目录映射与保留校验，历史材料集中为 ZIP，避免散落在工作目录。
