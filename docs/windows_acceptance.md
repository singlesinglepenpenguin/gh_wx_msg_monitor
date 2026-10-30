# Windows acceptance, 2026-10-30

**Real Windows initialization, group reading and desktop Excel update passed.** Client: Windows Weixin 4.1.10.31, AMD64, installed under `D:/Weixin`. Account: the user-confirmed current Windows account, `wxid_963al809bcox22`, not the previously selected Mac account.

The old test selected another account's `message_0.db` as its HMAC oracle. Diagnostic logs also confirmed that the debuggee was suspended while checking approximately 40 candidates, taking 15–19 seconds per breakpoint even with CNG. Earlier managed .NET PBKDF2 timing was approximately 1982 ms per candidate. This explains observed pauses; an anti-debug explanation remains unproved.

The recorded Windows compatibility patch uses CNG PBKDF2 with identical inputs, SHA512, 256000 rounds and 32-byte output, retaining the original managed fallback. Synthetic equivalence checks pass. Process launch, authorization, breakpoints, candidate selection/order and HMAC criteria remain upstream behavior. We did not separately retest the slow original backend with the corrected account, so neither the performance patch nor account selection is claimed as the sole cause.

## Real acceptance evidence

| Check | Result |
|---|---|
| One-time upstream active initialization | 32/32 database keys verified and saved |
| Core database refresh/integrity | 9 required databases passed |
| Group location | Exactly one GH派点反馈群 among 1707 sessions |
| Initial live query | 18 relay messages; longest content 754 characters |
| Real Excel update on reference copy | 29 cells cleared; original reference and roster unchanged |
| Normal Weixin restart and Tk GUI update | Saved credentials reused; final run read 23 live messages after synchronization; 29 cells cleared |
| GUI result and continuation | Success shown; new output selected; state saved |
| Windows display scaling | DPI handling and initial height fixed; all main controls verified visible |
| Review items | 4 grouped items for institutions absent from the original reference table |

Reports: `data/diagnostics/windows_live_validation.json`, `windows_live_gui.json`, `windows_live_workbook.json`. Screenshot: `data/diagnostics/windows_gui_success.png`. Independent workbook verification confirmed exactly the 29 reported cells were cleared, styles were preserved and every other ZIP member was unchanged. Excel copies and per-run TXT evidence are under `data/validation`. No fabricated messages or keys were used. Production workbook progress was not advanced.

Initialization exits the test Weixin client as the upstream script specifies. Reopen Weixin normally and keep it logged in for daily operation; use `start.bat`. Existing valid credentials do not require another debugger or extraction.

## Current reproduction

The table above records historical acceptance. Its personal runtime artifacts were subsequently removed at the user's request. The one-off scripts with fixed reference paths, dates and acceptance-session assumptions have been retired; they are not current reproduction commands.

Use the GUI procedure in [the user manual](user_manual.md) with a copy of the desired workbook and the correct time interval. Existing credentials use normal daily reading; missing credentials use the GUI one-time setup. For repeatable offline coverage, run the regression suite, including test_schedule.py (simulated Beijing slots with real workers, queue, Excel writes and progress) and test_wechat_source.py (constructed encrypted databases/WAL, not a live account).

Source: user-provided `gh_ui_fresh/api/wechat_native`, commit `6d30d29603328412829013c59cd19f84343c8518`. Original and patch SHA256 values remain in `vendor/UPSTREAM.json`; `scripts/verify_vendor.py` rejects unrecorded changes and extra files. This acceptance does not establish support for Weixin 4.1.15.56 or other computers/accounts.
