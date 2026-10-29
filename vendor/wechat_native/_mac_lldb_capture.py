"""lldb 脚本: 抓取微信 4.1.x SQLCipher 密钥 (口令 passphrase + 派生 enc_key).

在 **lldb 自带的 Python 解释器内**运行 (此时 `import lldb` 可用, 无需配 PYTHONPATH);
由 wechat_native/scanner_mac.py::extract_db_keys_lldb 通过命令行驱动:

    lldb -b \
      -o "process attach --pid <PID>" \
      -o "command script import <此文件>" \
      -o "wx_capture <timeout_sec>"

背景 (见 ylytdeng/wechat-decrypt#96 + 本机 4.1.10 二进制静态分析):
  * 微信 4.0/4.1 的数据库引擎是 roam_server.framework / roam_migration.framework (WCDB 改名),
    其内嵌 SQLCipher **用 CommonCrypto** 实现密码学原语 (二者都 import _CCKeyDerivationPBKDF /
    _CCCryptorCreate / _CCHmac*), 全部在主进程 (WeChat) 内, 符号已 strip。
  * 4.1.x 内存不再缓存 raw key, 改为开库时 PBKDF2-SHA512(口令, 每库 salt, 256000) 现场派生。
  * 微信 **不调用导出的 sqlite3_key** (DB 框架里根本没有该符号) —— 对它下断永远不命中。

为什么抓 CommonCrypto + 用断点回调 (4.1.10 实测教训):
  * --waitfor 在启动早期 attach, 对共享缓存里的 CC 符号下断会"延迟解析", 微信登录时很早就
    开库派生 key, 断点还没就位即被错过 (实测 0 命中)。改为 attach **已运行且已登录**的微信:
    共享缓存早已映射, 断点立即就位; 数据库已开、正被读取, 用户滚动聊天即持续触发 AES。
  * `lldb -b` 批处理下自建 listener 的事件循环收不到断点 stop 事件 (实测命中也 0)。改用
    SBBreakpoint.SetScriptCallbackFunction 注册回调: lldb 命中时直接回调, 不依赖事件循环;
    回调 return False = 不停下、自动继续, 既不漏也基本不拖慢微信。

双断点策略:
  1. CCCryptorCreate (AES) —— 每解密一页都调用, key 形参就是该库派生好的 enc_key。
     arm64: CCCryptorCreate(op x0, alg x1, options x2, key x3, keyLen x4, iv x5, ref x6)。
     过滤 alg==0(AES) && keyLen==32 收集 32 字节 enc_key 候选 (按值去重)。**主力**: 运行中
     的微信只要读库就命中, 不依赖重启/重登。上层用每库 page1 salt 的 HMAC 校验认领。
  2. CCKeyDerivationPBKDF (PBKDF2) —— 仅开新库时调用一次。窗口内若有新库打开 (切换视图懒
     加载某库, 或登出再登入), 命中 rounds>=200000 那次, x1[0:x2] 即 32 字节口令。
     arm64: (algo x0, pwd x1, pwdLen x2, salt x3, saltLen x4, prf x5, rounds w6, dk x7, dkLen [sp])。
     口令**通用**: 拿到后上层对所有库 PBKDF2 派生即可, 比逐库 enc_key 更完整。

输出 (stdout, 驱动方解析):
  WECHAT_PASSPHRASE=<hex>   抓到的 32 字节口令 (最优, 通用)
  WECHAT_ENCKEY=<hex>       每个去重的 32 字节 enc_key 候选 (逐库, 需 salt 校验认领)
  WXLLDB: ...               诊断行

注意: 只读寄存器/内存, 不改目标进程; 命中足够或超时立即 detach (不杀微信)。一次性, 用小号。
"""
import shlex
import sys
import time

import lldb  # 仅在 lldb 解释器内可用

SENTINEL = "WECHAT_PASSPHRASE="
ENCKEY_SENTINEL = "WECHAT_ENCKEY="

_KDF_SYM = "CCKeyDerivationPBKDF"   # PBKDF2: 派生 enc_key (输入=口令); "通用口令"来源
_AES_SYM = "CCCryptorCreate"        # AES: 每页解密; key 形参=派生好的 enc_key; 运行中读库即命中

_KDF_MIN_ROUNDS = 200000   # enc_key 派生 256000 轮; mac_key 派生 2 轮 —— 按此阈值只抓口令那次
_ENCKEY_TARGET = 24        # 去重 enc_key 候选收集到这个数即可提前结束 (覆盖常见已开库)

# 命中状态由断点回调累积 (回调在 lldb 命中线程里执行, 单线程, 无需加锁)。
_STATE = {
    "passphrase": None,   # bytes | None
    "enckeys": [],        # 有序去重的 32B enc_key
    "enkset": set(),
    "aes_hits": 0,
    "kdf_hits": 0,
}


def _p(msg: str) -> None:
    """内部辅助函数: 处理  p 中间步骤，供微信本地数据流程复用。"""
    print("WXLLDB:", msg)
    sys.stdout.flush()


def _reg(frame, name: str) -> int:
    """内部辅助函数: 处理  reg 中间步骤，供微信本地数据流程复用。"""
    r = frame.FindRegister(name)
    return r.GetValueAsUnsigned() if r and r.IsValid() else 0


def _dump_bp_modules(bp, label: str) -> int:
    """内部辅助函数: 处理  dump bp modules 中间步骤，供微信本地数据流程复用。"""
    n = bp.GetNumLocations()
    mods = set()
    for i in range(n):
        loc = bp.GetLocationAtIndex(i)
        mod = loc.GetAddress().GetModule()
        fn = mod.GetFileSpec().GetFilename() if mod and mod.IsValid() else "?"
        if fn:
            mods.add(fn)
    where = ", ".join(sorted(mods)) if mods else "(未解析)"
    _p(f"符号 {label}: {n} 个位置 [{where}]")
    return n


def _aes_cb(frame, bp_loc, *_rest):
    """CCCryptorCreate 回调: alg==0(AES) && keyLen==32 时收 x3[0:32] 作 enc_key 候选。return False 不停。"""
    st = _STATE
    st["aes_hits"] += 1
    if len(st["enckeys"]) >= _ENCKEY_TARGET:
        return False
    alg = _reg(frame, "x1")
    keylen = _reg(frame, "x4")
    keyptr = _reg(frame, "x3")
    if alg == 0 and keylen == 32 and keyptr:
        process = frame.GetThread().GetProcess()
        err = lldb.SBError()
        data = process.ReadMemory(keyptr, 32, err)
        if err.Success() and data and len(data) == 32 and data not in st["enkset"]:
            st["enkset"].add(data)
            st["enckeys"].append(data)
    return False


def _kdf_cb(frame, bp_loc, *_rest):
    """CCKeyDerivationPBKDF 回调: rounds>=200000 (输入=口令) 时收 x1[0:x2] 作口令。return False 不停。"""
    st = _STATE
    st["kdf_hits"] += 1
    if st["passphrase"] is not None:
        return False
    rounds = _reg(frame, "x6") & 0xFFFFFFFF
    if rounds < _KDF_MIN_ROUNDS:
        return False
    pwlen = _reg(frame, "x2")
    pwptr = _reg(frame, "x1")
    if pwptr and 8 <= pwlen <= 64:
        process = frame.GetThread().GetProcess()
        err = lldb.SBError()
        data = process.ReadMemory(pwptr, pwlen, err)
        if err.Success() and data and len(data) == pwlen:
            st["passphrase"] = data
            _p(f"PBKDF2 命中: rounds={rounds} pwlen={pwlen} -> 取得口令")
    return False


def wx_capture(debugger, command, result, internal_dict):
    """lldb 命令入口: wx_capture <timeout_sec>。需已 attach 到微信主进程。"""
    argv = shlex.split(command or "")
    timeout = int(argv[0]) if argv and argv[0].isdigit() else 60

    # 复位状态 (同一 lldb 会话内可能多次调用)
    _STATE.update(passphrase=None, enckeys=[], enkset=set(), aes_hits=0, kdf_hits=0)

    target = debugger.GetSelectedTarget()
    if not target or not target.IsValid():
        _p("NO_TARGET: 未 attach 到目标进程 (attach 失败?)")
        return
    process = target.GetProcess()
    if not process or not process.IsValid():
        _p("NO_PROCESS: target 无 process")
        return
    _p(f"attached pid={process.GetProcessID()} exe={target.GetExecutable().GetFilename()}")

    aes_bp = target.BreakpointCreateByName(_AES_SYM)
    _dump_bp_modules(aes_bp, _AES_SYM)
    aes_bp.SetScriptCallbackFunction("_mac_lldb_capture._aes_cb")
    kdf_bp = target.BreakpointCreateByName(_KDF_SYM)
    _dump_bp_modules(kdf_bp, _KDF_SYM)
    kdf_bp.SetScriptCallbackFunction("_mac_lldb_capture._kdf_cb")
    # 不在此处对 0 位置 bail: 共享缓存符号可能"待解析", continue 后随 dylib 加载就位。

    _p(f"已下断点 (主: {_AES_SYM} 收 enc_key, 辅: {_KDF_SYM} 收口令); 等待 {timeout}s。"
       " 现在请在微信里【滚动聊天列表 / 打开几个会话】以触发数据库读取 (要更完整可【登出再登入】)。")

    debugger.SetAsync(True)
    continue_error = process.Continue()  # 异步放飞; 命中由回调累积, 主线程轮询 _STATE
    _p(f"continue: success={continue_error.Success()} error={continue_error.GetCString()} "
       f"state={process.GetState()} arch={target.GetTriple()}")

    deadline = time.time() + timeout
    while time.time() < deadline:
        if _STATE["passphrase"] is not None:
            break
        if len(_STATE["enckeys"]) >= _ENCKEY_TARGET:
            break
        time.sleep(0.2)

    passphrase = _STATE["passphrase"]
    enckeys = list(_STATE["enckeys"])
    aes_hits = _STATE["aes_hits"]
    kdf_hits = _STATE["kdf_hits"]
    _p(f"原生断点计数: aes={aes_bp.GetHitCount()} kdf={kdf_bp.GetHitCount()} "
       f"state={process.GetState()}")

    if passphrase:
        print(SENTINEL + passphrase.hex())
    for k in enckeys:
        print(ENCKEY_SENTINEL + k.hex())
    sys.stdout.flush()

    _p(f"统计: aes_hits={aes_hits} kdf_hits={kdf_hits} enc_key候选={len(enckeys)} 口令={'有' if passphrase else '无'}")
    if not passphrase and not enckeys:
        total_loc = aes_bp.GetNumLocations() + kdf_bp.GetNumLocations()
        if total_loc == 0:
            _p("NO_SYMBOL: CommonCrypto 符号始终未解析 (CCCryptorCreate/CCKeyDerivationPBKDF 均无位置)。")
        elif aes_hits == 0 and kdf_hits == 0:
            _p("0 命中: 断点已就位但窗口内微信未读库/未开库 —— 请在等待期间滚动聊天/打开会话; 或确认微信已登录。")
        else:
            _p("命中了 CommonCrypto 但未取到 32B 密钥 —— 寄存器约定可能需按真机调整, 请反馈上面统计。")

    try:
        process.Detach()
    except Exception:
        pass


def __lldb_init_module(debugger, internal_dict):
    """内部辅助函数: 处理   lldb init module 中间步骤，供微信本地数据流程复用。"""
    debugger.HandleCommand(
        "command script add -f _mac_lldb_capture.wx_capture wx_capture"
    )
