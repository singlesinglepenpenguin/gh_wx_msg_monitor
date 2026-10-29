"""wechat_native 包: 微信数据本地解密 / 查询 / 分析的纯 Python 实现.

子模块分工:

核心模块 (gh_ui 自研):
  crypto.py      -- SQLCipher 4 协议解密 (AES-256-CBC + HMAC-SHA512);
                    提供 PAGE_SZ / SALT_SZ 常量及 decrypt_page / open_db 接口.
  key_scan.py    -- 跨平台通用: HMAC-SHA512 验证候选密钥 + DB 文件枚举 + hex 模式匹配;
                    不依赖平台 API, 由 scanner_*.py 和 _mac_helper.py 调用.
  scanner_win.py -- Windows 专用: ctypes 调用 ReadProcessMemory 扫描 Weixin.exe 内存;
                    无需重签名, 普通管理员权限即可.
  scanner_mac.py -- macOS 专用: 通过 osascript+sudo 驱动 _mac_helper.py, 使用
                    task_for_pid + mach_vm_read 扫描微信进程; 需先 resign_wechat().
  messages.py    -- 解密后的 message_*.db 查询 + 会话列表聚合; 依赖 crypto.py.

扩展模块 (从 gh_wx 移植):
  stock_kline.py         -- akshare K 线 + pyecharts 可视化 + 聊天悬停标注.
  stock_filter.py        -- 聚源 secumain 拉股票名称 + 高效模糊匹配;
                            需调用 configure_databases() 注入 JYDB_ENGINES.
  dat_to_image.py        -- 微信 V4 .dat 图片解密 (AES-128 + XOR 两种模式).
  wechat_key_extractor.py-- 从微信进程提取图片 AES/XOR 密钥 (仅 Windows).
  llm_client.py          -- 7 家 LLM 提供商统一客户端 (OpenAI/Qwen/DeepSeek/Claude/...).
  pdf_reporter.py        -- fpdf2 + 思源黑体, 跨平台生成中文 PDF 报告.

设计前提:
  * 不依赖 sqlcipher3-binary (Win wheel 不稳), 用 pycryptodome 自解密 SQLCipher 页.
  * 股票数据走 gh_quant_ui 主项目的 JYDB_ENGINES, 通过
    stock_filter.configure_databases() 在运行时注入, 避免循环依赖.
  * 非当前平台的 scanner_*.py 可安全 import, 调用时才抛 RuntimeError.
"""

__all__ = [
    "crypto",
    "key_scan",
    "scanner_win",
    "scanner_mac",
    "messages",
    "stock_kline",
    "stock_filter",
    "dat_to_image",
    "wechat_key_extractor",
    "llm_client",
    "pdf_reporter",
]
