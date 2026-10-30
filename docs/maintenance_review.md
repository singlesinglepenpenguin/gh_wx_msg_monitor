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
