# Windows validation, 2026-10-30

**Current result: real Windows initialization, group reading and desktop Excel update passed.** See [current acceptance](windows_acceptance.md). The failed 2026-10-29 attempt below is retained as historical evidence and is superseded for the tested 4.1.10.31/current-account combination.

## Historical attempt, 2026-10-29

Source: user-provided `gh_ui_fresh/api/wechat_native`, commit `6d30d29603328412829013c59cd19f84343c8518`.

The Windows scanner and PowerShell scripts are present and match that upstream. The application now uses the same active-first/raw-fallback order as `gh_ui/api/wechat.py`. It refuses initialization while Weixin is running and does not terminate the user's running client. Windows initialization is an explicit one-time action; daily reads use saved credentials.

## Real WeChat test

Client: Windows Weixin 4.1.15.56, AMD64, installed under `D:/Weixin`.

1. The initial attempt correctly returned WECHAT_RUNNING.
2. The user exited Weixin through its tray icon.
3. Original extraction found the installed executable, DLL and selected account's message_0.db.
4. Original static analysis found two key-set candidates, RVA 0x6148A0 and 0x2695460.
5. Original DEBUG_PROCESS startup installed both breakpoints. The user reported the client stayed at “正在进入”.
6. The transcript eventually reported no master key. The upstream raw fallback then reported Weixin was no longer running.
7. Both test processes were confirmed absent after the session ended. No credentials were generated, no real messages were read, and no real Excel update was performed.

This historical attempt did not pass. An anti-debug or version compatibility explanation was a hypothesis, not an established cause. The later successful acceptance with the correct account supersedes this earlier failure as the current result.

## Independent checks

The dependency check, GUI, discovery of two local account directories, and synthetic Excel update passed. Regression tests cover native call order, refusal to fall back on WECHAT_RUNNING, redaction, preservation of original failures, and all bundled file fingerprints. These checks do not establish live key extraction or live message reading.

The full original and patched SHA256 values are in `vendor/UPSTREAM.json`; `scripts/verify_vendor.py` rejects unrecorded changes and extra files. `.gitattributes` keeps vendor files at LF on Windows. Windows patches suppress inherited console key output, surface redacted failures, add stage counters, and use the equivalent Windows CNG PBKDF2 backend with original .NET fallback. They change no process startup, authorization, breakpoint locations, candidate selection or acquisition triggers.

## Follow-up, 2026-10-30

The user identified 4.1.10.31 as a colleague's known working version. A signed installer with SHA256 `62b1e23decaa358398b7863d1528696af0aeb8a75cb99d250793aac705c5e312` was verified (Tencent Authenticode: Valid) and installed with user approval. The original 4.1.15.56 program directory is backed up locally.

Added diagnostics proved that an actual key-set breakpoint was reached: the original managed PBKDF2 backend checked 32 candidates in 51.5 seconds. Earlier absence of a success log did not establish zero native breakpoint hits. The diagnostic run was interrupted by a server restart and produced no credentials.

Two synthetic vectors, including zero bytes, matched the original managed PBKDF2-HMAC-SHA512 output byte for byte using Windows CNG at the same 256000 rounds and 32-byte output. Timings were approximately 0.42–0.49 seconds versus 1.9–2.0 seconds. This is a computation backend compatibility change, not a different key acquisition method. Original validation modes, candidate order and fallback remain in place.

The subsequent live run completed its first unsuccessful breakpoint check in 18.9 seconds (45 candidates), the second in 17.0 seconds (42 candidates), then reached a third breakpoint. Live key extraction and message reading are still pending; these intermediate observations alone are not a success claim.
