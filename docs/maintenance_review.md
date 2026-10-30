# Maintenance review, 2026-10-30

Baseline `bd67694` was committed and pushed before changes. Functional changes were committed separately as `641a0db`; cleanup is a separate commit so each can be reviewed or reverted independently.

Removed development scripts:

| Files | Reason and retained replacement |
| --- | --- |
| capture_ui.py | macOS-only screenshot and UI assertions bound to an old saved-credential setup; current fresh GUI and Windows smoke checks remain |
| record_cold_setup.py, windows_gui_driver.ps1 | require a deleted cold_setup_session.json, fixed key count and fixed historical session artifacts |
| validate_windows_live.py, validate_windows_gui.py | hardcoded 2026-10-26 interval and retired data/reference locations; use the documented GUI on an isolated workbook copy for actual account acceptance |
| verify_live_workbook.py | only verifies a specific historical windows_live_gui.json; ZIP/member/style and exact-cell preservation remain covered in test_workflow.py |
| verify_reference.py | fixes added rows to 10/11 and relies on historical reference paths; generic workbook fixtures cover additions, reordered rows and untouched ZIP members |
| windows_session_probe.ps1 | one-off debugger PID probe/termination and full-screen capture, unused by application, packaging or tests |

Application cleanup removes one unused key_scan import and Windows conditions after the Windows connection handler has already returned. The automatic post-initialization normal reopen helper is still used and retained. Legacy preferences/progress compatibility, macOS permission repair, CLI modes, upstream helpers and all current regression test files are retained.

Removed one duplicate, macOS-skipped vendor-hash test that hardcoded the patch-name set. The platform-independent manifest verification test still checks all recorded originals, patches and extra files. The meaningful initialization, encryption/WAL, matching, Excel preservation and scheduling tests remain.

The clean-environment helper now passes setup.bat --quiet on Windows so dependency setup does not wait for a user to close a GUI before continuing its own checks. Documentation no longer presents removed scripts or deleted personal artifacts as current reproduction steps.

No user runtime data or reference workbooks are deleted by this cleanup. No vendor source or upstream acquisition flow is changed. Packaging includes application code and upstream dependencies, not the removed developer scripts. Final validation covers regressions, vendor fingerprints, naming, real GUI layout and packaged dependencies; simulated messages/time are distinguished from live WeChat access.

Final results: 95 regression cases ran (94 passed, one macOS-specific case skipped). Before removing the duplicate skipped test, the functional commit ran 96 cases with the same 94 passes. An isolated real source GUI verified manual-start guidance, no launch button, visible remaining connection controls and no inherited credentials. The rebuilt binary passed dependency checks; source and bundled vendor trees matched all 11 manifest entries. Existing account credentials, preferences, outputs, records and reference workbooks were retained.

## 接续摘要与可复用经验

- 已认可版本与提交：bd67694 为修改前基线；641a0db 修复定时更新和移除手动打开微信按钮；e2dede7 清理失效脚本；162487d 修正客户包诊断文档隐私。均已推送至 feat_init_win。
- 客户交付：dist/GroupMessageMonitor_windows.zip。完整解压，首次 setup.bat，自行登录微信、识别账号、完成一次性连接；日常 start.bat。保持微信登录、工具和电脑运行才会自动更新。用户已收到编号操作步骤。
- 机构解析：先判断消息主体与明确机构字段，再对应每轮重读的 Excel 机构行；正文提及、括号、日期、表情不能单独授权更新。多主体、未知主体、证据冲突均保留待核对。目标表缺少机构时 0 格是正确结果；实际清空测试必须提供对应机构、姓名列和非空内容。
- 调度：北京时间 09:00–09:04 和 15:00–15:04，各窗口一次；忙时不并发，错过整个窗口不补跑。自动失败不弹模态框，保留编号、处理办法和日志；结束与异常后恢复队列、busy 和定时检查。模拟时间贯通真实 worker/queue/Excel 的测试不能称为真实到点或微信读取成功。
- 隐私教训：没有凭据文件不等于没有个人信息。开发诊断、截图和文档也会包含账号、路径。客户指南与开发历史必须分离；verify_distribution.py 检查 ZIP 运行数据与 Markdown 内的账号/本机路径。当前包已替换客户指南，98 项测试中 97 项通过、1 项平台跳过，依赖与上游指纹检查通过。
- 清理教训：先建立可恢复基线，证明引用关系和替代覆盖后再删除。保留有效回归测试、旧数据兼容、上游初始化流程和两个 reference 测试 Excel。构建目录也可能被用户实际运行而生成个人数据，重建前须识别并保留，不应直接视为可丢弃缓存。
- 后续约束：不改上游采集方式，不重复抓钥，不记录完整候选密钥；运行故障不得被写日志失败覆盖。客户 ZIP 外的 data 和开发记录不应随应用交付。
